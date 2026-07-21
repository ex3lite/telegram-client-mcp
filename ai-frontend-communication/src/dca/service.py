from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from dca.db import (
    ChangeRequest,
    Clarification,
    ProjectMembership,
    ServiceAccount,
    ServiceAccountProject,
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


class ServiceError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


def project_member_profile(user: User, membership: ProjectMembership) -> dict[str, str | None]:
    return {
        "display_name": user.display_name,
        "role": membership.role,
        "department": membership.department,
        "stack": membership.stack,
    }


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
