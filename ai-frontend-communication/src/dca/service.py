from __future__ import annotations

import base64
import hashlib
import hmac
import re
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import and_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from dca.db import (
    AgentMessage,
    ChangeRequest,
    Clarification,
    ProjectAgentSettings,
    ProjectMembership,
    ServiceAccount,
    ServiceAccountProject,
    SystemSecret,
    TelegramChat,
    TelegramIdentity,
    User,
    append_audit,
    enqueue_job,
)
from dca.domain import (
    AskUserInput,
    ChangeRequestCreate,
    ChangeRequestStatus,
    ClarificationResult,
    ClarificationStatus,
    InvalidStateTransition,
    ensure_transition,
    utcnow,
)
from dca.privacy import PrivacyLevel, sanitize_text

SYSTEM_SECRET_CLAUDE_OAUTH = "claude_oauth_token"  # noqa: S105 - database key, not a secret
_MARKDOWN_ATTACHMENT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.md")


class ServiceError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.metadata = metadata or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


def admin_key_fingerprint(access_key: UUID, server_secret: str) -> bytes:
    return hmac.new(
        server_secret.encode(),
        b"dca-admin-access-v1\0" + access_key.bytes,
        hashlib.sha256,
    ).digest()


def validate_admin_access_key(access_key: UUID) -> UUID:
    if access_key.version != 4:
        raise ValueError("admin access key must be UUIDv4")
    return access_key


def encrypt_system_secret(value: str, server_secret: str) -> bytes:
    if not value:
        raise ValueError("system secret cannot be empty")
    return _system_secret_cipher(server_secret).encrypt(value.encode())


def decrypt_system_secret(ciphertext: bytes, server_secret: str) -> str:
    try:
        return _system_secret_cipher(server_secret).decrypt(ciphertext).decode()
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise ServiceError(
            "secret_unavailable", "Stored system secret cannot be decrypted"
        ) from exc


async def load_system_secret(
    session: AsyncSession,
    name: str,
    server_secret: str,
) -> str | None:
    secret = await session.get(SystemSecret, name)
    if secret is None:
        return None
    return decrypt_system_secret(secret.ciphertext, server_secret)


def _system_secret_cipher(server_secret: str) -> Fernet:
    if len(server_secret) < 32:
        raise ValueError("server secret must contain at least 32 characters")
    digest = hashlib.sha256(b"dca-system-secret-v1\0" + server_secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def project_member_profile(user: User, membership: ProjectMembership) -> dict[str, str | None]:
    return {
        "display_name": user.display_name,
        "role": membership.role,
        "department": membership.department,
        "stack": membership.stack,
    }


async def load_project_agent_settings(
    session: AsyncSession,
    project_id: UUID,
) -> ProjectAgentSettings:
    stored = await session.get(ProjectAgentSettings, project_id)
    if stored is not None:
        return stored
    return ProjectAgentSettings(
        project_id=project_id,
        enabled=True,
        claude_model=None,
        claude_effort="medium",
        claude_timeout_seconds=180,
        max_budget_cents=None,
        base_prompt="",
        answer_style="normal",
        privacy_level="strict",
        denied_globs=[],
        telegram_group_mode="mentions",
        telegram_private_mode="all_messages",
        telegram_attach_markdown=True,
        version=0,
        updated_by_admin_id=None,
    )


async def require_service_scope(
    session: AsyncSession,
    *,
    service_account_id: UUID,
    project_id: UUID,
    tool: str,
) -> ServiceAccount:
    account = await session.scalar(
        select(ServiceAccount)
        .join(
            ServiceAccountProject,
            ServiceAccountProject.service_account_id == ServiceAccount.id,
        )
        .where(
            ServiceAccount.id == service_account_id,
            ServiceAccountProject.project_id == project_id,
            ServiceAccount.active.is_(True),
        )
    )
    now = utcnow()
    if account is None or (account.expires_at is not None and account.expires_at <= now):
        raise ServiceError("forbidden", "Service account cannot access this project")
    if tool not in account.tool_scopes:
        raise ServiceError("forbidden", "Service account cannot use this tool")
    return account


async def create_agent_message(
    session: AsyncSession,
    *,
    service_account_id: UUID,
    project_id: UUID,
    correlation_id: str,
    idempotency_key: str,
    target_user_id: UUID | None,
    target_chat_id: UUID | None,
    text_markdown: str,
    attachment_name: str | None,
    attachment_markdown: str | None,
) -> tuple[AgentMessage, bool]:
    await require_service_scope(
        session,
        service_account_id=service_account_id,
        project_id=project_id,
        tool="telegram.send_message",
    )
    if target_user_id is None and target_chat_id is None:
        project_chats = list(
            await session.scalars(
                select(TelegramChat)
                .where(
                    TelegramChat.project_id == project_id,
                    TelegramChat.enabled.is_(True),
                )
                .order_by(TelegramChat.created_at)
                .limit(2)
            )
        )
        if not project_chats:
            raise ServiceError("chat_unavailable", "Project has no enabled Telegram chat")
        if len(project_chats) > 1:
            raise ServiceError("chat_ambiguous", "Specify an internal target_chat_id")
        target_chat_id = project_chats[0].id
    validate_agent_message(
        correlation_id=correlation_id,
        idempotency_key=idempotency_key,
        target_user_id=target_user_id,
        target_chat_id=target_chat_id,
        text_markdown=text_markdown,
        attachment_name=attachment_name,
        attachment_markdown=attachment_markdown,
    )
    project_settings = await load_project_agent_settings(session, project_id)
    if not project_settings.enabled:
        raise ServiceError("agent_disabled", "The project agent is disabled")
    privacy_level = cast(PrivacyLevel, project_settings.privacy_level)
    text_result = sanitize_text(
        text_markdown,
        level=privacy_level,
        location="agent_message.text",
    )
    attachment_result = (
        sanitize_text(
            attachment_markdown,
            level=privacy_level,
            location="agent_message.attachment",
        )
        if attachment_markdown is not None
        else None
    )
    if text_result.blocked or (attachment_result is not None and attachment_result.blocked):
        blocked_findings = [
            *text_result.findings,
            *(attachment_result.findings if attachment_result is not None else []),
        ]
        raise ServiceError(
            "privacy_blocked",
            "Message contains protected credential material",
            metadata={
                "privacy_findings_count": len(blocked_findings),
                "privacy_findings": [
                    {"kind": finding["kind"], "location": finding["location"]}
                    for finding in blocked_findings
                ],
            },
        )
    text_markdown = text_result.text
    attachment_markdown = attachment_result.text if attachment_result is not None else None
    privacy_findings = [
        *text_result.findings,
        *(attachment_result.findings if attachment_result is not None else []),
    ]
    existing = await session.scalar(
        select(AgentMessage).where(
            AgentMessage.service_account_id == service_account_id,
            AgentMessage.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        ensure_same_agent_message(
            existing,
            project_id=project_id,
            correlation_id=correlation_id,
            target_user_id=target_user_id,
            target_chat_id=target_chat_id,
            text_markdown=text_markdown,
            attachment_name=attachment_name,
            attachment_markdown=attachment_markdown,
        )
        return existing, False

    if target_user_id is not None:
        recipient = await session.scalar(
            select(TelegramIdentity)
            .join(ProjectMembership, ProjectMembership.user_id == TelegramIdentity.user_id)
            .where(
                ProjectMembership.project_id == project_id,
                TelegramIdentity.user_id == target_user_id,
                TelegramIdentity.verified_at.is_not(None),
                TelegramIdentity.reachable.is_(True),
                TelegramIdentity.private_chat_id.is_not(None),
            )
        )
        if recipient is None:
            raise ServiceError(
                "recipient_unreachable",
                "Recipient is not a verified, reachable member of this project",
            )
    else:
        chat = await session.scalar(
            select(TelegramChat).where(
                TelegramChat.id == target_chat_id,
                TelegramChat.project_id == project_id,
                TelegramChat.enabled.is_(True),
            )
        )
        if chat is None:
            raise ServiceError("chat_unavailable", "Target chat is unavailable to this project")

    message_id = uuid4()
    statement = (
        insert(AgentMessage)
        .values(
            id=message_id,
            project_id=project_id,
            service_account_id=service_account_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            target_user_id=target_user_id,
            target_chat_id=target_chat_id,
            text_markdown=text_markdown,
            attachment_name=attachment_name,
            attachment_markdown=attachment_markdown,
            privacy_findings=privacy_findings,
            status="queued",
        )
        .on_conflict_do_nothing(
            index_elements=[AgentMessage.service_account_id, AgentMessage.idempotency_key]
        )
        .returning(AgentMessage.id)
    )
    inserted = (await session.execute(statement)).scalar_one_or_none()
    if inserted is None:
        raced = await session.scalar(
            select(AgentMessage).where(
                AgentMessage.service_account_id == service_account_id,
                AgentMessage.idempotency_key == idempotency_key,
            )
        )
        if raced is None:
            raise ServiceError("internal_error", "Idempotency conflict could not be resolved")
        ensure_same_agent_message(
            raced,
            project_id=project_id,
            correlation_id=correlation_id,
            target_user_id=target_user_id,
            target_chat_id=target_chat_id,
            text_markdown=text_markdown,
            attachment_name=attachment_name,
            attachment_markdown=attachment_markdown,
        )
        return raced, False

    message = await session.get(AgentMessage, message_id)
    if message is None:
        raise ServiceError("internal_error", "Agent message was not persisted")
    await enqueue_job(
        session,
        kind="telegram.deliver_agent_message",
        payload={"agent_message_id": str(message.id)},
        deduplication_key=f"agent-message:{message.id}:deliver",
    )
    await append_audit(
        session,
        event_type="agent_message.created",
        correlation_id=correlation_id,
        actor_type="service_account",
        actor_id=str(service_account_id),
        project_id=project_id,
        subject_type="agent_message",
        subject_id=str(message.id),
        payload={
            "target_type": "user" if target_user_id is not None else "chat",
            "target_id": str(target_user_id or target_chat_id),
            "has_attachment": attachment_name is not None,
            "privacy_findings_count": len(privacy_findings),
            "privacy_findings": [
                {"kind": finding["kind"], "location": finding["location"]}
                for finding in privacy_findings
            ],
        },
    )
    return message, True


def validate_agent_message(
    *,
    correlation_id: str,
    idempotency_key: str,
    target_user_id: UUID | None,
    target_chat_id: UUID | None,
    text_markdown: str,
    attachment_name: str | None,
    attachment_markdown: str | None,
) -> None:
    if (target_user_id is None) == (target_chat_id is None):
        raise ServiceError("invalid_target", "Exactly one message target is required")
    if not 1 <= len(correlation_id) <= 255 or not 1 <= len(idempotency_key) <= 255:
        raise ServiceError("invalid_request", "Correlation and idempotency keys are required")
    if not 1 <= len(text_markdown) <= 4_096 or "\0" in text_markdown:
        raise ServiceError("invalid_message", "Message must contain 1-4096 safe characters")
    if (attachment_name is None) != (attachment_markdown is None):
        raise ServiceError(
            "invalid_attachment", "Attachment name and content must be supplied together"
        )
    if attachment_name is None:
        return
    if len(text_markdown) > 1_024:
        raise ServiceError(
            "invalid_message", "Messages with a Markdown attachment are limited to 1024 characters"
        )
    if not is_safe_markdown_attachment_name(attachment_name):
        raise ServiceError("invalid_attachment", "Only a safe .md filename is supported")
    if not attachment_markdown or len(attachment_markdown.encode()) > 1_048_576:
        raise ServiceError("invalid_attachment", "Markdown attachment exceeds the 1 MiB limit")


def is_safe_markdown_attachment_name(value: str) -> bool:
    return ".." not in value and _MARKDOWN_ATTACHMENT_NAME_RE.fullmatch(value) is not None


def ensure_same_agent_message(
    message: AgentMessage,
    *,
    project_id: UUID,
    correlation_id: str,
    target_user_id: UUID | None,
    target_chat_id: UUID | None,
    text_markdown: str,
    attachment_name: str | None,
    attachment_markdown: str | None,
) -> None:
    if (
        message.project_id != project_id
        or message.correlation_id != correlation_id
        or message.target_user_id != target_user_id
        or message.target_chat_id != target_chat_id
        or message.text_markdown != text_markdown
        or message.attachment_name != attachment_name
        or message.attachment_markdown != attachment_markdown
    ):
        raise ServiceError(
            "idempotency_conflict",
            "The idempotency key was already used for a different message",
        )


async def create_clarification(
    session: AsyncSession,
    *,
    service_account_id: UUID,
    request: AskUserInput,
) -> tuple[Clarification, bool]:
    await require_service_scope(
        session,
        service_account_id=service_account_id,
        project_id=request.project_id,
        tool="telegram.ask_user",
    )
    now = utcnow()
    if request.expires_at <= now:
        raise ServiceError("request_expired", "The requested expiry is already in the past")

    existing = await session.scalar(
        select(Clarification).where(
            Clarification.service_account_id == service_account_id,
            Clarification.idempotency_key == request.idempotency_key,
        )
    )
    if existing is not None:
        ensure_same_clarification_request(existing, request)
        return existing, False

    recipient = await session.execute(
        select(TelegramIdentity)
        .join(ProjectMembership, ProjectMembership.user_id == TelegramIdentity.user_id)
        .where(
            ProjectMembership.project_id == request.project_id,
            TelegramIdentity.user_id == request.recipient_user_id,
            TelegramIdentity.verified_at.is_not(None),
            TelegramIdentity.reachable.is_(True),
            TelegramIdentity.private_chat_id.is_not(None),
        )
    )
    if recipient.scalar_one_or_none() is None:
        raise ServiceError(
            "recipient_unreachable",
            "Recipient is not a verified, reachable member of this project",
        )

    clarification_id = uuid4()
    statement = (
        insert(Clarification)
        .values(
            id=clarification_id,
            project_id=request.project_id,
            service_account_id=service_account_id,
            recipient_user_id=request.recipient_user_id,
            agent_run_id=request.agent_run_id,
            correlation_id=request.correlation_id,
            idempotency_key=request.idempotency_key,
            context=request.context,
            question=request.question,
            expected_answer=request.expected_answer,
            status=ClarificationStatus.PENDING.value,
            expires_at=request.expires_at,
        )
        .on_conflict_do_nothing(
            index_elements=[
                Clarification.service_account_id,
                Clarification.idempotency_key,
            ]
        )
        .returning(Clarification.id)
    )
    inserted = (await session.execute(statement)).scalar_one_or_none()
    if inserted is None:
        raced = await session.scalar(
            select(Clarification).where(
                Clarification.service_account_id == service_account_id,
                Clarification.idempotency_key == request.idempotency_key,
            )
        )
        if raced is None:
            raise ServiceError("internal_error", "Idempotency conflict could not be resolved")
        ensure_same_clarification_request(raced, request)
        return raced, False

    clarification = await session.get(Clarification, clarification_id)
    if clarification is None:
        raise ServiceError("internal_error", "Clarification was not persisted")
    await enqueue_job(
        session,
        kind="telegram.deliver_clarification",
        payload={"clarification_id": str(clarification.id)},
        deduplication_key=f"clarification:{clarification.id}:deliver",
    )
    await append_audit(
        session,
        event_type="clarification.created",
        correlation_id=request.correlation_id,
        actor_type="service_account",
        actor_id=str(service_account_id),
        project_id=request.project_id,
        subject_type="clarification",
        subject_id=str(clarification.id),
        payload={"recipient_user_id": str(request.recipient_user_id)},
    )
    return clarification, True


async def get_clarification(
    session: AsyncSession,
    *,
    service_account_id: UUID,
    request_id: UUID,
    for_update: bool = False,
) -> Clarification:
    statement = select(Clarification).where(
        Clarification.id == request_id,
        Clarification.service_account_id == service_account_id,
    )
    if for_update:
        statement = statement.with_for_update()
    clarification = await session.scalar(statement)
    if clarification is None:
        raise ServiceError("forbidden", "Clarification is unavailable")
    await require_service_scope(
        session,
        service_account_id=service_account_id,
        project_id=clarification.project_id,
        tool="telegram.get_clarification",
    )
    if (
        clarification.status == ClarificationStatus.PENDING.value
        and clarification.expires_at <= utcnow()
    ):
        if await expire_clarification(session, clarification):
            await session.refresh(clarification)
    return clarification


async def cancel_clarification(
    session: AsyncSession,
    *,
    service_account_id: UUID,
    request_id: UUID,
    reason: str | None,
) -> Clarification:
    clarification = await get_clarification(
        session,
        service_account_id=service_account_id,
        request_id=request_id,
        for_update=True,
    )
    if clarification.status == ClarificationStatus.CANCELLED.value:
        return clarification
    try:
        ensure_transition(ClarificationStatus(clarification.status), ClarificationStatus.CANCELLED)
    except InvalidStateTransition as exc:
        raise ServiceError("invalid_state_transition", str(exc)) from exc

    result = await session.execute(
        update(Clarification)
        .where(
            Clarification.id == clarification.id,
            Clarification.status == ClarificationStatus.PENDING.value,
        )
        .values(
            status=ClarificationStatus.CANCELLED.value,
            cancelled_reason=(reason or "")[:2_000],
            updated_at=utcnow(),
        )
        .returning(Clarification.id)
    )
    if result.scalar_one_or_none() is None:
        raise ServiceError("invalid_state_transition", "Clarification changed concurrently")
    await append_audit(
        session,
        event_type="clarification.cancelled",
        correlation_id=clarification.correlation_id,
        actor_type="service_account",
        actor_id=str(service_account_id),
        project_id=clarification.project_id,
        subject_type="clarification",
        subject_id=str(clarification.id),
        payload={"reason": (reason or "")[:500]},
    )
    if clarification.telegram_message_id is not None:
        await enqueue_job(
            session,
            kind="telegram.notify_clarification_cancelled",
            payload={"clarification_id": str(clarification.id)},
            deduplication_key=f"clarification:{clarification.id}:cancelled",
        )
    await session.refresh(clarification)
    return clarification


async def expire_clarification(session: AsyncSession, clarification: Clarification) -> bool:
    result = await session.execute(
        update(Clarification)
        .where(
            Clarification.id == clarification.id,
            Clarification.status == ClarificationStatus.PENDING.value,
            Clarification.expires_at <= utcnow(),
        )
        .values(status=ClarificationStatus.EXPIRED.value, updated_at=utcnow())
        .returning(Clarification.id)
    )
    if result.scalar_one_or_none() is None:
        return False
    await append_audit(
        session,
        event_type="clarification.expired",
        correlation_id=clarification.correlation_id,
        actor_type="system",
        actor_id="worker",
        project_id=clarification.project_id,
        subject_type="clarification",
        subject_id=str(clarification.id),
    )
    return True


async def answer_clarification_from_telegram(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    telegram_chat_id: int,
    reply_to_message_id: int,
    answer: str,
) -> Clarification:
    now = utcnow()
    clarification = await session.scalar(
        select(Clarification)
        .join(TelegramIdentity, TelegramIdentity.user_id == Clarification.recipient_user_id)
        .where(
            TelegramIdentity.telegram_user_id == telegram_user_id,
            Clarification.telegram_chat_id == telegram_chat_id,
            Clarification.telegram_message_id == reply_to_message_id,
        )
    )
    if clarification is None:
        raise ServiceError("request_not_found", "No clarification is bound to this reply")
    if clarification.expires_at <= now:
        await expire_clarification(session, clarification)
        raise ServiceError("request_expired", "This clarification has expired")
    result = await session.execute(
        update(Clarification)
        .where(
            Clarification.id == clarification.id,
            Clarification.status == ClarificationStatus.PENDING.value,
            Clarification.expires_at > now,
        )
        .values(
            status=ClarificationStatus.ANSWERED.value,
            answer_raw=answer[:16_000],
            answered_at=now,
            updated_at=now,
        )
        .returning(Clarification.id)
    )
    if result.scalar_one_or_none() is None:
        raise ServiceError("invalid_state_transition", "This clarification is already closed")
    await append_audit(
        session,
        event_type="clarification.answered",
        correlation_id=clarification.correlation_id,
        actor_type="user",
        actor_id=str(clarification.recipient_user_id),
        project_id=clarification.project_id,
        subject_type="clarification",
        subject_id=str(clarification.id),
        payload={"answer_length": len(answer)},
    )
    await session.refresh(clarification)
    return clarification


def ensure_same_clarification_request(
    clarification: Clarification,
    request: AskUserInput,
) -> None:
    matches = (
        clarification.project_id == request.project_id
        and clarification.recipient_user_id == request.recipient_user_id
        and clarification.agent_run_id == request.agent_run_id
        and clarification.correlation_id == request.correlation_id
        and clarification.context == request.context
        and clarification.question == request.question
        and clarification.expected_answer == request.expected_answer
        and clarification.expires_at == request.expires_at
    )
    if not matches:
        raise ServiceError(
            "idempotency_conflict",
            "The idempotency key was already used for a different request",
        )


def clarification_result(clarification: Clarification) -> ClarificationResult:
    return ClarificationResult(
        request_id=clarification.id,
        status=ClarificationStatus(clarification.status),
        answer=clarification.answer_raw,
        answered_at=clarification.answered_at,
        expires_at=clarification.expires_at,
    )


async def create_change_request(
    session: AsyncSession,
    *,
    request: ChangeRequestCreate,
    correlation_id: str,
    source: str,
    source_ref: dict[str, Any],
    created_by_user_id: UUID | None,
) -> ChangeRequest:
    change_request = ChangeRequest(
        project_id=request.project_id,
        created_by_user_id=created_by_user_id,
        correlation_id=correlation_id,
        source=source,
        source_ref=source_ref,
        kind=request.kind,
        title=request.title,
        description=request.description,
        priority=request.priority,
    )
    session.add(change_request)
    await session.flush()
    await append_audit(
        session,
        event_type="request.created",
        correlation_id=correlation_id,
        actor_type="user" if created_by_user_id else "system",
        actor_id=str(created_by_user_id or "unknown"),
        project_id=request.project_id,
        subject_type="change_request",
        subject_id=str(change_request.id),
        payload={"kind": request.kind, "priority": request.priority, "source": source},
    )
    return change_request


async def update_change_request_status(
    session: AsyncSession,
    *,
    request_id: UUID,
    target: ChangeRequestStatus,
    expected_version: int,
    actor_id: str,
) -> ChangeRequest:
    change_request = await session.get(ChangeRequest, request_id)
    if change_request is None:
        raise ServiceError("request_not_found", "Change request was not found")
    try:
        ensure_transition(ChangeRequestStatus(change_request.status), target)
    except InvalidStateTransition as exc:
        raise ServiceError("invalid_state_transition", str(exc)) from exc
    result = await session.execute(
        update(ChangeRequest)
        .where(
            ChangeRequest.id == request_id,
            ChangeRequest.version == expected_version,
        )
        .values(status=target.value, version=expected_version + 1, updated_at=utcnow())
        .returning(ChangeRequest.id)
    )
    if result.scalar_one_or_none() is None:
        raise ServiceError("version_conflict", "Change request changed concurrently")
    await append_audit(
        session,
        event_type="request.status_changed",
        correlation_id=change_request.correlation_id,
        actor_type="admin",
        actor_id=actor_id,
        project_id=change_request.project_id,
        subject_type="change_request",
        subject_id=str(change_request.id),
        payload={"from": change_request.status, "to": target.value},
    )
    await session.refresh(change_request)
    return change_request


async def list_expired_pending(session: AsyncSession, *, limit: int = 100) -> list[Clarification]:
    result = await session.scalars(
        select(Clarification)
        .where(
            and_(
                Clarification.status == ClarificationStatus.PENDING.value,
                Clarification.expires_at <= datetime.now(UTC),
            )
        )
        .order_by(Clarification.expires_at)
        .limit(limit)
    )
    return list(result)
