from __future__ import annotations

import asyncio
import os
import random
import re
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from typing import Any, Literal, cast
from uuid import UUID, uuid4

import structlog
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramConflictError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from sqlalchemy import select, text, update
from sqlalchemy.exc import SQLAlchemyError

from dca.claude import (
    ClaudeCode,
    ClaudeError,
    RepositorySnapshots,
    compile_agent_policy,
)
from dca.config import Settings, get_settings
from dca.db import (
    AgentMessage,
    ConversationThread,
    Database,
    Interaction,
    Job,
    ProjectMembership,
    Repository,
    TelegramChat,
    TelegramIdentity,
    TelegramUpdate,
    User,
    append_audit,
    enqueue_job,
    enqueue_repository_sync,
)
from dca.domain import (
    AgentChangeRequestProposal,
    JobStatus,
    KnowledgeAnswer,
    KnowledgeArtifact,
    RepositoryStatus,
    utcnow,
)
from dca.memory import (
    ConversationContext,
    append_conversation_message,
    get_or_create_conversation_thread,
    load_conversation_context,
    upsert_conversation_memory,
)
from dca.privacy import (
    SECURITY_GUARD_ROLE,
    PrivacyFinding,
    PrivacyLevel,
    sanitize_agent_output,
    sanitize_text,
)
from dca.service import (
    SYSTEM_SECRET_CLAUDE_OAUTH,
    ServiceError,
    create_agent_change_request,
    expire_clarification,
    list_expired_pending,
    load_project_agent_settings,
    load_system_secret,
    project_member_profile,
)
from dca.telegram import TelegramAdapter, document_requested, ingest_telegram_update

log = structlog.get_logger()
TELEGRAM_EXTERNAL_ACTIONS = {
    "telegram.deliver_agent_message",
    "telegram.deliver_clarification",
    "telegram.notify_clarification_cancelled",
    "telegram.publish_interaction",
    "telegram.process_update",
}
TELEGRAM_POLL_TIMEOUT_SECONDS = 30
TELEGRAM_POLL_REQUEST_TIMEOUT_SECONDS = 90
TELEGRAM_POLL_RETRY_MAX_SECONDS = 30.0
TELEGRAM_POLL_LOCK_ID = 0x4443415F5447504C
TELEGRAM_POLL_FATAL_ERRORS = (
    TelegramBadRequest,
    TelegramConflictError,
    TelegramForbiddenError,
    TelegramUnauthorizedError,
)
KNOWLEDGE_STREAM_INTERVAL_SECONDS = 1.0
PRIVATE_DRAFT_KEEPALIVE_SECONDS = 15


def _poll_retry_delay(base_delay: float) -> float:
    return base_delay + random.uniform(0, base_delay * 0.2)  # noqa: S311 - retry jitter


def trusted_requester_profile(interaction: Interaction) -> dict[str, Any] | None:
    """Return the queue-time audit snapshot; never use it as authorization authority."""
    raw_profile = interaction.source_ref.get("requester_profile")
    if interaction.source != "telegram" or not isinstance(raw_profile, dict):
        return None
    profile: dict[str, Any] = {
        key: value
        for key in (
            "display_name",
            "role",
            "department",
            "stack",
            "language",
            "knowledge_scope",
            "can_create_requests",
        )
        if isinstance((value := raw_profile.get(key)), (str, bool))
    }
    telegram_user_id = interaction.source_ref.get("telegram_user_id")
    if isinstance(telegram_user_id, int) and not isinstance(telegram_user_id, bool):
        profile["telegram_user_id"] = telegram_user_id
    return profile or None


async def load_live_requester_profile(
    session: Any,
    interaction: Interaction,
) -> dict[str, Any] | None:
    if interaction.source != "telegram":
        return None
    telegram_user_id = interaction.source_ref.get("telegram_user_id")
    if not isinstance(telegram_user_id, int) or isinstance(telegram_user_id, bool):
        raise ServiceError(
            "project_scope_violation",
            "Requester Telegram identity is missing or no longer verified",
        )
    identity = await session.scalar(
        select(TelegramIdentity).where(
            TelegramIdentity.telegram_user_id == telegram_user_id,
            TelegramIdentity.verified_at.is_not(None),
        )
    )
    if identity is None:
        raise ServiceError(
            "project_scope_violation",
            "Requester Telegram identity is missing or no longer verified",
        )

    requester_user_id: UUID | None = None
    if interaction.conversation_thread_id is not None:
        requester_user_id = await session.scalar(
            select(ConversationThread.user_id).where(
                ConversationThread.id == interaction.conversation_thread_id,
                ConversationThread.project_id == interaction.project_id,
            )
        )
    if requester_user_id is None:
        raw_user_id = interaction.source_ref.get("requester_user_id")
        if isinstance(raw_user_id, str):
            with suppress(ValueError):
                requester_user_id = UUID(raw_user_id)
    if requester_user_id is not None and requester_user_id != identity.user_id:
        raise ServiceError(
            "project_scope_violation",
            "Requester Telegram identity no longer matches the conversation author",
        )
    requester_user_id = identity.user_id
    row = (
        await session.execute(
            select(User, ProjectMembership)
            .join(ProjectMembership, ProjectMembership.user_id == User.id)
            .where(
                User.id == requester_user_id,
                User.active.is_(True),
                ProjectMembership.project_id == interaction.project_id,
            )
        )
    ).one_or_none()
    if row is None:
        raise ServiceError(
            "project_scope_violation",
            "Requester no longer has access to this project",
        )
    profile = project_member_profile(*row)
    profile["user_id"] = str(requester_user_id)
    profile["telegram_user_id"] = telegram_user_id
    return profile


def interaction_delivery_scope(
    interaction: Interaction,
) -> Literal["private", "group", "external"]:
    delivery = interaction.source_ref.get("delivery")
    if interaction.source != "telegram" or not isinstance(delivery, dict):
        return "external"
    return "group" if delivery.get("kind") == "group_message" else "private"


def interaction_agent_role(
    interaction: Interaction,
) -> Literal["knowledge", "bydlo_guard"]:
    return (
        SECURITY_GUARD_ROLE
        if interaction.source_ref.get("agent_role") == SECURITY_GUARD_ROLE
        else "knowledge"
    )


def sanitize_stream_text(
    value: str,
    *,
    level: PrivacyLevel,
    location: str,
) -> tuple[str, list[PrivacyFinding]]:
    """Redact complete findings and hold the unfinished token at the stream edge."""
    result = sanitize_agent_output(value, level=level, location=location)
    private_key_start = value.find("-----BEGIN")
    if (
        not result.findings
        and private_key_start >= 0
        and "PRIVATE" in value[private_key_start:].upper()
    ):
        return (
            f"{value[:private_key_start]}[REDACTED:private_key]",
            [{"kind": "private_key", "location": location, "action": "redacted"}],
        )
    boundary = max(result.text.rfind(" "), result.text.rfind("\n"), result.text.rfind("\t"))
    return (
        result.text[: boundary + 1].rstrip() if boundary >= 0 else "",
        result.findings,
    )


def conversation_prompt_context(context: ConversationContext) -> dict[str, Any]:
    return {
        "summary": context.summary,
        "facts": [{"key": fact.key, "content": fact.content} for fact in context.facts],
        "messages": [
            {
                "role": message.role,
                "source": message.source,
                "content": message.content,
                "author_user_id": (
                    str(message.author_user_id) if message.author_user_id is not None else None
                ),
                "created_at": message.created_at.isoformat(),
            }
            for message in context.messages
        ],
    }


class Worker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.database = Database(settings)
        self.telegram = TelegramAdapter(settings, self.database)
        self.snapshots = RepositorySnapshots(settings)
        self.claude = ClaudeCode(settings)
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"
        self._last_expiry_sweep = 0.0
        self._last_repository_reconcile = 0.0

    async def run_forever(self) -> None:
        await self.recover_stale_jobs()
        log.info(
            "worker.started",
            worker_id=self.worker_id,
            telegram_mode=self.settings.telegram_mode,
        )
        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self._run_job_loop(exclude_kind="knowledge.answer"))
                for _ in range(self.settings.knowledge_concurrency):
                    tasks.create_task(self._run_job_loop(only_kind="knowledge.answer"))
                if self.settings.telegram_mode == "polling":
                    tasks.create_task(self._poll_telegram_forever())
        finally:
            await self.telegram.close()
            await self.database.close()

    async def _run_job_loop(
        self,
        *,
        only_kind: str | None = None,
        exclude_kind: str | None = None,
    ) -> None:
        while True:
            if only_kind is None:
                await self._sweep_expired_if_due()
                await self._reconcile_repositories_if_due()
            job = await self.claim_job(only_kind=only_kind, exclude_kind=exclude_kind)
            if job is None:
                await asyncio.sleep(self.settings.worker_poll_seconds)
                continue
            await self.process(job)

    async def _poll_telegram_batch(self, offset: int | None) -> int | None:
        updates = await self.telegram.bot.get_updates(
            offset=offset,
            timeout=TELEGRAM_POLL_TIMEOUT_SECONDS,
            allowed_updates=self.telegram.allowed_updates(),
            request_timeout=TELEGRAM_POLL_REQUEST_TIMEOUT_SECONDS,
        )
        for telegram_update in updates:
            payload = telegram_update.model_dump(mode="json", by_alias=True, exclude_none=True)
            async with self.database.session() as session:
                await ingest_telegram_update(
                    session,
                    self.telegram,
                    payload,
                    actor_id="polling-worker",
                )
            offset = telegram_update.update_id + 1
        return offset

    @asynccontextmanager
    async def _telegram_poll_lock(self) -> AsyncIterator[None]:
        async with self.database.engine.connect() as connection:
            acquired = await connection.scalar(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": TELEGRAM_POLL_LOCK_ID},
            )
            if acquired is not True:
                raise RuntimeError("another Telegram polling worker already holds the lock")
            log.info("telegram.poll_lock_acquired")
            try:
                yield
            finally:
                try:
                    released = await connection.scalar(
                        text("SELECT pg_advisory_unlock(:lock_id)"),
                        {"lock_id": TELEGRAM_POLL_LOCK_ID},
                    )
                except Exception as exc:
                    log.exception("telegram.poll_lock_release_failed")
                    await connection.invalidate(exc)
                else:
                    if released is not True:
                        log.error("telegram.poll_lock_release_failed")
                        await connection.invalidate()

    @asynccontextmanager
    async def _repository_sync_lock(self, repository_id: UUID) -> AsyncIterator[None]:
        lock_key = f"dca:repository-sync:{repository_id}"
        async with self.database.engine.connect() as connection:
            acquired = await connection.scalar(
                text("SELECT pg_try_advisory_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": lock_key},
            )
            if acquired is not True:
                raise ServiceError(
                    "repository_sync_busy",
                    "Repository sync is already running",
                    retryable=True,
                )
            try:
                yield
            finally:
                try:
                    released = await connection.scalar(
                        text("SELECT pg_advisory_unlock(hashtextextended(:lock_key, 0))"),
                        {"lock_key": lock_key},
                    )
                except Exception as exc:
                    log.exception(
                        "repository.sync_lock_release_failed",
                        repository_id=str(repository_id),
                    )
                    await connection.invalidate(exc)
                else:
                    if released is not True:
                        log.error(
                            "repository.sync_lock_release_failed",
                            repository_id=str(repository_id),
                        )
                        await connection.invalidate()

    @asynccontextmanager
    async def _conversation_context_lock(self, thread_id: UUID) -> AsyncIterator[None]:
        lock_key = f"dca:claude-context:{thread_id}"
        async with self.database.engine.connect() as connection:
            acquired = await connection.scalar(
                text("SELECT pg_try_advisory_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": lock_key},
            )
            if acquired is not True:
                raise ServiceError(
                    "conversation_context_busy",
                    "Another answer is already using this conversation context",
                    retryable=True,
                )
            try:
                yield
            finally:
                try:
                    released = await connection.scalar(
                        text("SELECT pg_advisory_unlock(hashtextextended(:lock_key, 0))"),
                        {"lock_key": lock_key},
                    )
                except Exception as exc:
                    log.exception(
                        "claude.context_lock_release_failed",
                        thread_id=str(thread_id),
                    )
                    await connection.invalidate(exc)
                else:
                    if released is not True:
                        log.error(
                            "claude.context_lock_release_failed",
                            thread_id=str(thread_id),
                        )
                        await connection.invalidate()

    async def _poll_telegram_forever(self) -> None:
        async with self._telegram_poll_lock():
            await self._delete_polling_webhook()
            await self._poll_telegram_loop()

    async def _delete_polling_webhook(self) -> None:
        retry_delay = 1.0
        while True:
            try:
                await self.telegram.bot.delete_webhook(drop_pending_updates=False)
            except asyncio.CancelledError:
                raise
            except TELEGRAM_POLL_FATAL_ERRORS as exc:
                log.error("telegram.poll_setup_fatal", error_type=type(exc).__name__)
                raise
            except TelegramRetryAfter as exc:
                delay = float(exc.retry_after)
                log.warning("telegram.poll_setup_rate_limited", retry_seconds=delay)
                await asyncio.sleep(delay)
            except (TelegramNetworkError, TelegramServerError) as exc:
                delay = _poll_retry_delay(retry_delay)
                log.warning(
                    "telegram.poll_setup_failed",
                    error_type=type(exc).__name__,
                    retry_seconds=delay,
                )
                await asyncio.sleep(delay)
                retry_delay = min(retry_delay * 2, TELEGRAM_POLL_RETRY_MAX_SECONDS)
            except TelegramAPIError as exc:
                log.error("telegram.poll_setup_fatal", error_type=type(exc).__name__)
                raise
            except Exception:
                log.exception("telegram.poll_setup_unexpected")
                raise
            else:
                log.info("telegram.poll_webhook_deleted", drop_pending_updates=False)
                return

    async def _poll_telegram_loop(self) -> None:
        offset: int | None = None
        retry_delay = 1.0
        while True:
            try:
                offset = await self._poll_telegram_batch(offset)
            except asyncio.CancelledError:
                raise
            except TELEGRAM_POLL_FATAL_ERRORS as exc:
                log.error("telegram.poll_fatal", error_type=type(exc).__name__)
                raise
            except TelegramRetryAfter as exc:
                delay = float(exc.retry_after)
                log.warning("telegram.poll_rate_limited", retry_seconds=delay)
                await asyncio.sleep(delay)
            except TelegramNetworkError as exc:
                delay = _poll_retry_delay(retry_delay)
                log.warning(
                    "telegram.poll_network_error",
                    error_type=type(exc).__name__,
                    retry_seconds=delay,
                )
                await asyncio.sleep(delay)
                retry_delay = min(retry_delay * 2, TELEGRAM_POLL_RETRY_MAX_SECONDS)
            except TelegramServerError as exc:
                delay = _poll_retry_delay(retry_delay)
                log.warning(
                    "telegram.poll_server_error",
                    error_type=type(exc).__name__,
                    retry_seconds=delay,
                )
                await asyncio.sleep(delay)
                retry_delay = min(retry_delay * 2, TELEGRAM_POLL_RETRY_MAX_SECONDS)
            except TelegramAPIError as exc:
                log.error("telegram.poll_fatal", error_type=type(exc).__name__)
                raise
            except SQLAlchemyError as exc:
                delay = _poll_retry_delay(retry_delay)
                log.warning(
                    "telegram.poll_database_error",
                    error_type=type(exc).__name__,
                    retry_seconds=delay,
                )
                await asyncio.sleep(delay)
                retry_delay = min(retry_delay * 2, TELEGRAM_POLL_RETRY_MAX_SECONDS)
            except Exception:
                log.exception("telegram.poll_unexpected")
                raise
            else:
                retry_delay = 1.0

    async def claim_job(
        self,
        *,
        only_kind: str | None = None,
        exclude_kind: str | None = None,
    ) -> Job | None:
        async with self.database.session() as session:
            statement = (
                select(Job)
                .where(
                    Job.status.in_([JobStatus.QUEUED.value, JobStatus.RETRY.value]),
                    Job.available_at <= utcnow(),
                )
                .order_by(Job.available_at, Job.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if only_kind is not None:
                statement = statement.where(Job.kind == only_kind)
            if exclude_kind is not None:
                statement = statement.where(Job.kind != exclude_kind)
            job = await session.scalar(statement)
            if job is None:
                return None
            job.status = JobStatus.RUNNING.value
            job.attempts += 1
            job.locked_at = utcnow()
            job.locked_by = self.worker_id
            await session.flush()
            session.expunge(job)
            return job

    async def process(self, job: Job) -> None:
        log.info("job.started", job_id=str(job.id), kind=job.kind, attempt=job.attempts)
        try:
            result = await self._dispatch(job)
        except TelegramRetryAfter as exc:
            if job.kind in TELEGRAM_EXTERNAL_ACTIONS:
                await self._delivery_uncertain(job, "telegram_rate_limited", str(exc))
            else:
                await self._retry(job, "telegram_rate_limited", str(exc), delay=exc.retry_after)
        except TelegramNetworkError as exc:
            if job.kind in TELEGRAM_EXTERNAL_ACTIONS:
                await self._delivery_uncertain(job, "telegram_network_timeout", str(exc))
            else:
                await self._retry(job, "telegram_network_error", str(exc))
        except (TelegramBadRequest, TelegramForbiddenError, TelegramUnauthorizedError) as exc:
            if job.kind == "telegram.deliver_agent_message":
                code = "telegram_delivery_rejected"
                detail = sanitize_text(str(exc), level="balanced", location="telegram_error").text
                await self._fail(job, code, detail)
                await self._fail_agent_message(job, code)
            elif job.kind in TELEGRAM_EXTERNAL_ACTIONS:
                await self._delivery_uncertain(job, "telegram_delivery_rejected", str(exc))
            else:
                await self._fail(job, "telegram_delivery_rejected", str(exc))
        except ClaudeError as exc:
            if exc.retryable:
                retry_scheduled = await self._retry(job, exc.code, exc.message)
                if not retry_scheduled:
                    await self._publish_interaction_error(job, exc.code)
            else:
                await self._fail(job, exc.code, exc.message)
                await self._publish_interaction_error(job, exc.code)
        except ServiceError as exc:
            if exc.retryable:
                contention_delay = {
                    "repository_sync_busy": 2,
                    "conversation_context_busy": 1,
                }.get(exc.code)
                await self._retry(
                    job,
                    exc.code,
                    exc.message,
                    delay=contention_delay,
                    consume_attempt=contention_delay is None,
                )
            else:
                await self._fail(job, exc.code, exc.message)
                if job.kind == "telegram.deliver_agent_message":
                    await self._fail_agent_message(job, exc.code)
        except Exception as exc:
            log.exception("job.unhandled_error", job_id=str(job.id), kind=job.kind)
            if job.kind in TELEGRAM_EXTERNAL_ACTIONS:
                await self._delivery_uncertain(job, "external_action_failed", type(exc).__name__)
            else:
                await self._retry(job, "internal_error", type(exc).__name__)
        else:
            await self._succeed(job, result)

    async def _dispatch(self, job: Job) -> dict[str, Any]:
        if job.kind == "telegram.process_update":
            update_id = int(job.payload["update_id"])
            async with self.database.session() as session:
                update_row = await session.get(TelegramUpdate, update_id)
                if update_row is None:
                    raise ServiceError("update_not_found", "Telegram update was not found")
                payload = dict(update_row.payload)
            await self.telegram.process_raw_update(payload)
            return {"update_id": update_id}
        if job.kind == "telegram.deliver_clarification":
            request_id = UUID(job.payload["clarification_id"])
            delivered = await self.telegram.deliver_clarification(request_id)
            return {"clarification_id": str(request_id), "accepted_by_telegram": delivered}
        if job.kind == "telegram.notify_clarification_cancelled":
            request_id = UUID(job.payload["clarification_id"])
            await self.telegram.notify_clarification_cancelled(request_id)
            return {"clarification_id": str(request_id), "accepted_by_telegram": True}
        if job.kind == "telegram.deliver_agent_message":
            agent_message_id = UUID(job.payload["agent_message_id"])
            return await self._deliver_agent_message(agent_message_id)
        if job.kind == "conversation.remember_agent_message":
            agent_message_id = UUID(job.payload["agent_message_id"])
            return await self._remember_agent_message(agent_message_id)
        if job.kind == "knowledge.answer":
            interaction_id = UUID(job.payload["interaction_id"])
            return await self._answer_interaction(interaction_id)
        if job.kind == "telegram.publish_interaction":
            interaction_id = UUID(job.payload["interaction_id"])
            return await self._publish_interaction(interaction_id)
        if job.kind == "repository.sync":
            repository_id = UUID(job.payload["repository_id"])
            generation = job.payload.get("generation", 0)
            if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
                raise ServiceError(
                    "repository_sync_generation_invalid",
                    "Repository sync generation is invalid",
                )
            return await self._sync_repository(
                repository_id,
                generation=generation,
                requested_commit=job.payload.get("requested_commit"),
                source=job.payload.get("source", "manual"),
            )
        raise ServiceError("unknown_job_kind", f"Unsupported job kind: {job.kind}")

    async def _deliver_agent_message(self, agent_message_id: UUID) -> dict[str, Any]:
        async with self.database.session() as session:
            message = await session.get(AgentMessage, agent_message_id)
            if message is None:
                raise ServiceError("message_not_found", "Agent message was not found")
            if message.status != "queued":
                return {
                    "agent_message_id": str(message.id),
                    "status": message.status,
                    "telegram_message_id": message.telegram_message_id,
                }
            agent_settings = await load_project_agent_settings(session, message.project_id)
            if not agent_settings.enabled:
                raise ServiceError("agent_disabled", "Agent is disabled for this project")
            if message.attachment_name is not None and not agent_settings.telegram_attach_markdown:
                raise ServiceError(
                    "attachments_disabled", "Markdown attachments are disabled for this project"
                )
            if message.target_user_id is not None:
                identity = await session.scalar(
                    select(TelegramIdentity)
                    .join(ProjectMembership, ProjectMembership.user_id == TelegramIdentity.user_id)
                    .where(
                        ProjectMembership.project_id == message.project_id,
                        TelegramIdentity.user_id == message.target_user_id,
                        TelegramIdentity.verified_at.is_not(None),
                        TelegramIdentity.reachable.is_(True),
                        TelegramIdentity.private_chat_id.is_not(None),
                    )
                )
                if identity is None or identity.private_chat_id is None:
                    raise ServiceError(
                        "recipient_unreachable", "Agent message recipient is unavailable"
                    )
                chat_id = identity.private_chat_id
                message_thread_id = None
            else:
                chat = await session.get(TelegramChat, message.target_chat_id)
                if chat is None or chat.project_id != message.project_id or not chat.enabled:
                    raise ServiceError("chat_unavailable", "Agent message chat is unavailable")
                chat_id = chat.telegram_chat_id
                message_thread_id = chat.message_thread_id

            if agent_settings.privacy_level not in {"strict", "balanced"}:
                raise ServiceError("privacy_policy_invalid", "Project privacy policy is invalid")
            level = cast(PrivacyLevel, agent_settings.privacy_level)
            text_result = sanitize_agent_output(
                message.text_markdown,
                level=level,
                location="agent_message.text_markdown",
            )
            attachment_result = (
                sanitize_agent_output(
                    message.attachment_markdown,
                    level=level,
                    location=f"agent_message.attachment:{message.attachment_name}",
                )
                if message.attachment_markdown is not None
                else None
            )
            findings = [
                *text_result.findings,
                *(attachment_result.findings if attachment_result is not None else []),
            ]
            if text_result.blocked or (attachment_result is not None and attachment_result.blocked):
                message.status = "failed"
                message.error_code = "privacy_blocked"
                message.privacy_findings = [
                    *message.privacy_findings,
                    *(dict(finding) for finding in findings),
                ]
                await append_audit(
                    session,
                    event_type="agent_message.privacy_blocked",
                    correlation_id=message.correlation_id,
                    actor_type="system",
                    actor_id="privacy-filter",
                    project_id=message.project_id,
                    subject_type="agent_message",
                    subject_id=str(message.id),
                    outcome="blocked",
                    payload=privacy_audit_payload(findings),
                )
                return {
                    "agent_message_id": str(message.id),
                    "status": "failed",
                    "privacy_blocked": True,
                }
            message.text_markdown = text_result.text
            if attachment_result is not None:
                message.attachment_markdown = attachment_result.text
            if findings:
                message.privacy_findings = [
                    *message.privacy_findings,
                    *(dict(finding) for finding in findings),
                ]
            await session.flush()
            session.expunge(message)

        telegram_message_id = await self.telegram.deliver_agent_message(
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            text_markdown=message.text_markdown,
            attachment_name=message.attachment_name,
            attachment_markdown=message.attachment_markdown,
        )
        async with self.database.session() as session:
            persisted = await session.get(AgentMessage, agent_message_id)
            if persisted is None:
                raise ServiceError("message_not_found", "Agent message disappeared")
            persisted.status = "sent"
            persisted.telegram_message_id = telegram_message_id
            persisted.error_code = None
            await append_audit(
                session,
                event_type="agent_message.delivered",
                correlation_id=persisted.correlation_id,
                actor_type="system",
                actor_id="telegram-worker",
                project_id=persisted.project_id,
                subject_type="agent_message",
                subject_id=str(persisted.id),
                payload={"telegram_message_id": telegram_message_id},
            )
            await enqueue_job(
                session,
                kind="conversation.remember_agent_message",
                payload={"agent_message_id": str(persisted.id)},
                deduplication_key=f"agent-message:{persisted.id}:remember",
                max_attempts=5,
            )
        return {
            "agent_message_id": str(agent_message_id),
            "status": "sent",
            "telegram_message_id": telegram_message_id,
        }

    async def _remember_agent_message(self, agent_message_id: UUID) -> dict[str, Any]:
        async with self.database.session() as session:
            message = await session.get(AgentMessage, agent_message_id)
            if message is None or message.status != "sent":
                return {"agent_message_id": str(agent_message_id), "remembered": False}
            agent_settings = await load_project_agent_settings(session, message.project_id)
            if not agent_settings.memory_enabled:
                return {"agent_message_id": str(agent_message_id), "remembered": False}
            thread = await get_or_create_conversation_thread(
                session,
                project_id=message.project_id,
                chat_id=message.target_chat_id,
                user_id=message.target_user_id,
            )
            _, created = await append_conversation_message(
                session,
                project_id=message.project_id,
                chat_id=message.target_chat_id,
                user_id=message.target_user_id,
                thread_id=thread.id,
                role="agent",
                source="mcp",
                content=message.text_markdown,
                external_id=str(message.id),
            )
            return {
                "agent_message_id": str(agent_message_id),
                "thread_id": str(thread.id),
                "remembered": True,
                "created": created,
            }

    async def _answer_interaction(self, interaction_id: UUID) -> dict[str, Any]:
        async with self.database.session() as session:
            interaction = await session.get(Interaction, interaction_id)
            if interaction is None:
                raise ServiceError("request_not_found", "Interaction is missing")
            thread_id = interaction.conversation_thread_id
        if thread_id is None:
            return await self._answer_interaction_impl(interaction_id)
        async with self._conversation_context_lock(thread_id):
            return await self._answer_interaction_impl(interaction_id)

    async def _answer_interaction_impl(self, interaction_id: UUID) -> dict[str, Any]:
        prompt_memory: dict[str, Any] | None = None
        claude_prompt_memory: dict[str, Any] | None = None
        native_session_id: UUID | None = None
        resume_session = False
        profile_changed_since_queue = False
        async with self.database.session() as session:
            interaction = await session.get(Interaction, interaction_id)
            if interaction is None:
                raise ServiceError("request_not_found", "Interaction is missing")
            if interaction.status in {"answer_ready", "published"}:
                return {
                    "interaction_id": str(interaction.id),
                    "already_completed": True,
                    "privacy_blocked": False,
                }
            if interaction.status == "failed" and interaction.error_code == "privacy_blocked":
                return {
                    "interaction_id": str(interaction.id),
                    "already_completed": True,
                    "privacy_blocked": True,
                }
            if interaction.repository_id is None:
                raise ServiceError("request_not_found", "Interaction or repository is missing")
            repository = await session.get(Repository, interaction.repository_id)
            if repository is None or interaction.commit_sha is None:
                raise ServiceError("source_unavailable", "Repository snapshot is unavailable")
            agent_settings = await load_project_agent_settings(session, interaction.project_id)
            if not agent_settings.enabled:
                raise ServiceError("agent_disabled", "Agent is disabled for this project")
            if agent_settings.privacy_level not in {"strict", "balanced"}:
                raise ServiceError("privacy_policy_invalid", "Project privacy policy is invalid")
            oauth_token = await load_system_secret(
                session,
                SYSTEM_SECRET_CLAUDE_OAUTH,
                self.settings.session_secret.get_secret_value(),
            )
            requester_profile = await load_live_requester_profile(session, interaction)
            queued_profile = trusted_requester_profile(interaction)
            if requester_profile is not None and queued_profile is not None:
                profile_changed_since_queue = any(
                    requester_profile.get(key) != queued_profile.get(key) for key in queued_profile
                )
            delivery_scope = interaction_delivery_scope(interaction)
            agent_role = interaction_agent_role(interaction)
            compiled_policy = compile_agent_policy(
                project_settings=agent_settings,
                requester_profile=requester_profile,
                delivery_scope=delivery_scope,
                repository_allowed_paths=repository.allowed_paths or [],
                repository_denied_globs=agent_settings.denied_globs or [],
                agent_role=agent_role,
            )
            thread: ConversationThread | None = None
            if agent_settings.memory_enabled and interaction.conversation_thread_id is not None:
                thread = await session.get(
                    ConversationThread,
                    interaction.conversation_thread_id,
                )
                if thread is None or thread.project_id != interaction.project_id:
                    raise ServiceError(
                        "conversation_scope_unavailable",
                        "Interaction conversation is unavailable",
                    )
                context = await load_conversation_context(
                    session,
                    project_id=interaction.project_id,
                    chat_id=thread.chat_id,
                    user_id=thread.user_id,
                    thread_id=thread.id,
                    message_limit=agent_settings.memory_recent_messages,
                    max_chars=agent_settings.memory_max_context_chars,
                    exclude_external_id=interaction.correlation_id,
                    before=interaction.created_at,
                )
                prompt_memory = conversation_prompt_context(context)
                resume_session = bool(
                    thread.claude_session_id is not None
                    and thread.claude_repository_id == repository.id
                    and thread.claude_commit_sha == interaction.commit_sha
                    and thread.claude_policy_hash == compiled_policy.policy_sha256
                )
                native_session_id = thread.claude_session_id if resume_session else uuid4()
            claude_prompt_memory = None if resume_session else prompt_memory
            interaction.status = "generating"
            await session.flush()
            session.expunge(interaction)
            session.expunge(repository)

        snapshot = await self.snapshots.materialize(
            repository,
            interaction.commit_sha,
            denied_globs=agent_settings.denied_globs,
        )
        wants_document = agent_role == "knowledge" and document_requested(interaction.question)
        policy_stale = asyncio.Event()

        async def ensure_policy_current() -> None:
            if policy_stale.is_set():
                raise ClaudeError(
                    "context_policy_changed",
                    "Requester access or agent context changed during generation",
                    retryable=True,
                )
            try:
                async with self.database.session() as session:
                    live_interaction = await session.get(Interaction, interaction_id)
                    if (
                        live_interaction is None
                        or live_interaction.repository_id != repository.id
                        or live_interaction.commit_sha != interaction.commit_sha
                    ):
                        raise ClaudeError(
                            "context_policy_changed",
                            "Interaction repository context changed during generation",
                            retryable=True,
                        )
                    live_repository = await session.get(Repository, repository.id)
                    if live_repository is None:
                        raise ClaudeError(
                            "context_policy_changed",
                            "Repository access changed during generation",
                            retryable=True,
                        )
                    live_settings = await load_project_agent_settings(
                        session, live_interaction.project_id
                    )
                    if not live_settings.enabled:
                        raise ClaudeError(
                            "context_policy_changed",
                            "Agent access changed during generation",
                            retryable=True,
                        )
                    live_profile = await load_live_requester_profile(session, live_interaction)
                    live_policy = compile_agent_policy(
                        project_settings=live_settings,
                        requester_profile=live_profile,
                        delivery_scope=interaction_delivery_scope(live_interaction),
                        repository_allowed_paths=live_repository.allowed_paths or [],
                        repository_denied_globs=live_settings.denied_globs or [],
                        agent_role=interaction_agent_role(live_interaction),
                    )
            except ClaudeError as exc:
                policy_stale.set()
                if exc.code == "context_policy_changed":
                    raise
                raise ClaudeError(
                    "context_policy_changed",
                    "Requester access or agent context changed during generation",
                    retryable=True,
                ) from exc
            except ServiceError as exc:
                policy_stale.set()
                raise ClaudeError(
                    "context_policy_changed",
                    "Requester access or agent context changed during generation",
                    retryable=True,
                ) from exc
            if live_policy.policy_sha256 != compiled_policy.policy_sha256:
                policy_stale.set()
                raise ClaudeError(
                    "context_policy_changed",
                    "Requester access or agent context changed during generation",
                    retryable=True,
                )

        last_stream_at = float("-inf")
        latest_stream_answer = ""
        latest_stream_thinking = ""
        stream_privacy_findings: set[tuple[str, str]] = set()
        stream_delivery_lock = asyncio.Lock()

        async def refresh_stream() -> bool:
            async with stream_delivery_lock:
                if not latest_stream_answer and not latest_stream_thinking:
                    await self.telegram.send_knowledge_progress(interaction)
                    return False
                delivery = await self.telegram.send_knowledge_stream(
                    interaction,
                    answer_markdown=latest_stream_answer,
                    thinking=latest_stream_thinking,
                )
                if not isinstance(delivery, dict):
                    return False
                current_delivery = interaction.source_ref.get("delivery")
                if delivery != current_delivery:
                    await self._persist_stream_delivery(interaction, delivery)
                return bool(latest_stream_answer) and delivery.get("kind") in {
                    "group_message",
                    "private_message",
                }

        heartbeat = (
            asyncio.create_task(
                self._draft_heartbeat(
                    interaction,
                    policy_guard=ensure_policy_current,
                    refresh=refresh_stream,
                )
            )
            if agent_settings.telegram_streaming_enabled
            else None
        )

        async def stream_answer(answer_markdown: str, thinking: str) -> None:
            nonlocal heartbeat, last_stream_at, latest_stream_answer, latest_stream_thinking
            safe_answer_stream, answer_stream_findings = sanitize_stream_text(
                answer_markdown,
                level=cast(PrivacyLevel, agent_settings.privacy_level),
                location="stream.answer_markdown",
            )
            safe_thinking_stream, thinking_stream_findings = sanitize_stream_text(
                thinking,
                level=cast(PrivacyLevel, agent_settings.privacy_level),
                location="stream.thinking",
            )
            stream_privacy_findings.update(
                (finding["kind"], finding["location"])
                for finding in (*answer_stream_findings, *thinking_stream_findings)
            )
            if not safe_answer_stream and not safe_thinking_stream:
                return
            latest_stream_answer = safe_answer_stream
            latest_stream_thinking = safe_thinking_stream
            now = asyncio.get_running_loop().time()
            if now - last_stream_at < KNOWLEDGE_STREAM_INTERVAL_SECONDS:
                return
            await ensure_policy_current()
            last_stream_at = now
            try:
                durable = await refresh_stream()
            except Exception:
                log.warning(
                    "telegram.stream_failed",
                    interaction_id=str(interaction.id),
                    exc_info=True,
                )
                return
            if durable and heartbeat is not None:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
                heartbeat = None

        async def ask_claude(*, should_resume: bool, memory: dict[str, Any] | None) -> Any:
            return await self.claude.answer(
                snapshot=snapshot,
                question=interaction.question,
                project_settings=agent_settings,
                requester_profile=requester_profile,
                conversation_context=memory,
                oauth_token=oauth_token,
                on_stream=(stream_answer if agent_settings.telegram_streaming_enabled else None),
                delivery_scope=delivery_scope,
                session_id=native_session_id,
                resume_session=should_resume,
                compiled_policy=compiled_policy,
                tool_profile=("none" if agent_role == SECURITY_GUARD_ROLE else "read_only"),
                artifact_requested=wants_document,
            )

        session_rotated = False
        try:
            try:
                result = await ask_claude(
                    should_resume=resume_session,
                    memory=claude_prompt_memory,
                )
            except ClaudeError as exc:
                if not resume_session or exc.code != "claude_session_unavailable":
                    raise
                native_session_id = uuid4()
                resume_session = False
                session_rotated = True
                result = await ask_claude(should_resume=False, memory=prompt_memory)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat

        await ensure_policy_current()
        if wants_document != bool(result.answer.artifacts):
            raise ClaudeError(
                "model_output_contract_violation",
                "Claude artifact output did not match the explicit document request",
                retryable=True,
            )
        answer = result.answer
        safe_answer, privacy_findings, privacy_blocked = sanitize_knowledge_answer(
            answer,
            level=cast(PrivacyLevel, agent_settings.privacy_level),
        )
        if agent_role == SECURITY_GUARD_ROLE:
            safe_answer = safe_answer.model_copy(
                update={"answer_markdown": normalize_guard_reply(safe_answer.answer_markdown)}
            )
        accepted = [check.citation.model_dump(mode="json") for check in result.accepted_citations]
        rejected = [check.model_dump(mode="json") for check in result.rejected_citations]
        if privacy_blocked:
            async with self.database.session() as session:
                persisted = await session.get(Interaction, interaction_id)
                if persisted is None:
                    raise ServiceError("request_not_found", "Interaction disappeared")
                persisted.status = "failed"
                persisted.error_code = "privacy_blocked"
                persisted.answer_markdown = None
                persisted.artifacts = []
                persisted.privacy_findings = [dict(finding) for finding in privacy_findings]
                persisted.citations = accepted
                persisted.rejected_citations = rejected
                await enqueue_job(
                    session,
                    kind="telegram.publish_interaction",
                    payload={"interaction_id": str(interaction_id)},
                    deduplication_key=f"interaction:{interaction_id}:publish",
                    max_attempts=3,
                )
                await append_audit(
                    session,
                    event_type="knowledge.answer_privacy_blocked",
                    correlation_id=persisted.correlation_id,
                    actor_type="system",
                    actor_id="privacy-filter",
                    project_id=persisted.project_id,
                    subject_type="interaction",
                    subject_id=str(persisted.id),
                    outcome="blocked",
                    payload=privacy_audit_payload(privacy_findings),
                )
            return {
                "interaction_id": str(interaction_id),
                "privacy_blocked": True,
                "findings": len(privacy_findings),
            }
        change_request_id: UUID | None = None
        proposal_suppressed = False
        async with self.database.session() as session:
            persisted = await session.get(Interaction, interaction_id)
            if persisted is None:
                raise ServiceError("request_not_found", "Interaction disappeared")
            persisted.status = "answer_ready"
            persisted.artifacts = serialize_artifacts(safe_answer.artifacts)
            persisted.privacy_findings = [dict(finding) for finding in privacy_findings]
            persisted.citations = accepted
            persisted.rejected_citations = rejected
            persisted.uncertainty = safe_answer.uncertainty
            answer_markdown = safe_answer.answer_markdown
            if (
                agent_role == "knowledge"
                and safe_answer.change_request is not None
                and compiled_policy.requester["can_create_requests"] is True
            ):
                try:
                    change_request, _ = await create_agent_change_request(
                        session,
                        interaction=persisted,
                        proposal=safe_answer.change_request,
                    )
                except ServiceError as exc:
                    if exc.code != "request_intent_required":
                        raise
                    proposal_suppressed = True
                else:
                    change_request_id = change_request.id
                    answer_markdown = (
                        f"{answer_markdown.rstrip()}\n\n"
                        f"📥 Создал заявку `{str(change_request.id)[:8]}` для backend-отдела."
                    )
            persisted.answer_markdown = render_answer(
                answer_markdown=answer_markdown,
                uncertainty=safe_answer.uncertainty,
            )
            persisted.provider_metadata = {
                "provider": "claude-code-cli",
                "cli_version": result.cli_version,
                "model": agent_settings.claude_model,
                "effort": agent_settings.claude_effort,
                "agent_role": agent_role,
                "answer_scope": safe_answer.answer_scope,
                "memory_enabled": prompt_memory is not None,
                "memory_messages": len(prompt_memory["messages"]) if prompt_memory else 0,
                "memory_summary_updated": bool(
                    persisted.conversation_thread_id is not None and safe_answer.memory_summary
                ),
                "document_requested": wants_document,
                "profile_changed_since_queue": profile_changed_since_queue,
                "session_rotated_after_resume_failure": session_rotated,
                "stream_privacy_findings": len(stream_privacy_findings),
                "native_context": result.context_metadata,
                "change_request_id": (
                    str(change_request_id) if change_request_id is not None else None
                ),
                "proposal_suppressed": proposal_suppressed,
            }
            if agent_settings.memory_enabled and persisted.conversation_thread_id is not None:
                thread = await session.get(
                    ConversationThread,
                    persisted.conversation_thread_id,
                )
                if thread is None or thread.project_id != persisted.project_id:
                    raise ServiceError(
                        "conversation_scope_unavailable",
                        "Interaction conversation is unavailable",
                    )
                if native_session_id is not None and result.session_id == str(native_session_id):
                    thread.claude_session_id = native_session_id
                    thread.claude_repository_id = persisted.repository_id
                    thread.claude_commit_sha = persisted.commit_sha
                    thread.claude_policy_hash = compiled_policy.policy_sha256
                    thread.claude_context_validated_at = utcnow()
                    if result.compaction_count:
                        thread.claude_compaction_count += result.compaction_count
                        thread.claude_last_compacted_at = utcnow()
                await append_conversation_message(
                    session,
                    project_id=persisted.project_id,
                    chat_id=thread.chat_id,
                    user_id=thread.user_id,
                    thread_id=thread.id,
                    role="assistant",
                    source="claude",
                    content=conversation_message_record(safe_answer.answer_markdown),
                    external_id=str(persisted.id),
                )
                if safe_answer.memory_summary:
                    stored_memory = await upsert_conversation_memory(
                        session,
                        project_id=persisted.project_id,
                        chat_id=thread.chat_id,
                        user_id=thread.user_id,
                        thread_id=thread.id,
                        kind="summary",
                        memory_key="current",
                        content=safe_answer.memory_summary,
                    )
                    if stored_memory.privacy_findings:
                        await append_audit(
                            session,
                            event_type="conversation.memory_redacted",
                            correlation_id=persisted.correlation_id,
                            actor_type="system",
                            actor_id="memory-filter",
                            project_id=persisted.project_id,
                            subject_type="conversation_thread",
                            subject_id=str(thread.id),
                            payload={
                                "findings": len(stored_memory.privacy_findings),
                                "kinds": sorted(
                                    {
                                        str(finding.get("kind", "unknown"))
                                        for finding in stored_memory.privacy_findings
                                    }
                                ),
                                "locations": sorted(
                                    {
                                        str(finding.get("location", "memory"))
                                        for finding in stored_memory.privacy_findings
                                    }
                                ),
                            },
                        )
            await enqueue_job(
                session,
                kind="telegram.publish_interaction",
                payload={"interaction_id": str(interaction_id)},
                deduplication_key=f"interaction:{interaction_id}:publish",
                max_attempts=3,
            )
            await append_audit(
                session,
                event_type="knowledge.answer_generated",
                correlation_id=persisted.correlation_id,
                actor_type="system",
                actor_id="claude-worker",
                project_id=persisted.project_id,
                subject_type="interaction",
                subject_id=str(persisted.id),
                payload={
                    "commit": persisted.commit_sha,
                    "accepted_citations": len(accepted),
                    "rejected_citations": len(rejected),
                    "cli_version": result.cli_version,
                },
            )
            await append_audit(
                session,
                event_type="claude.context_validated",
                correlation_id=persisted.correlation_id,
                actor_type="system",
                actor_id="claude-worker",
                project_id=persisted.project_id,
                subject_type="interaction",
                subject_id=str(persisted.id),
                payload={
                    "contract_version": result.context_metadata.get("contract_version"),
                    "policy_sha256": compiled_policy.policy_sha256,
                    "session_id": result.session_id,
                    "resumed": resume_session,
                    "compaction_count": result.compaction_count,
                    "profile_changed_since_queue": profile_changed_since_queue,
                },
            )
            if result.compaction_count:
                await append_audit(
                    session,
                    event_type="claude.context_compacted",
                    correlation_id=persisted.correlation_id,
                    actor_type="system",
                    actor_id="claude-worker",
                    project_id=persisted.project_id,
                    subject_type="interaction",
                    subject_id=str(persisted.id),
                    payload={
                        "count": result.compaction_count,
                        "context_attested_after_compaction": True,
                    },
                )
            if privacy_findings:
                await append_audit(
                    session,
                    event_type="knowledge.answer_privacy_redacted",
                    correlation_id=persisted.correlation_id,
                    actor_type="system",
                    actor_id="privacy-filter",
                    project_id=persisted.project_id,
                    subject_type="interaction",
                    subject_id=str(persisted.id),
                    payload=privacy_audit_payload(privacy_findings),
                )
            if proposal_suppressed:
                await append_audit(
                    session,
                    event_type="knowledge.change_request_proposal_suppressed",
                    correlation_id=persisted.correlation_id,
                    actor_type="system",
                    actor_id="request-intent-gate",
                    project_id=persisted.project_id,
                    subject_type="interaction",
                    subject_id=str(persisted.id),
                    outcome="suppressed",
                    payload={"reason": "request_intent_required"},
                )
            if stream_privacy_findings:
                await append_audit(
                    session,
                    event_type="knowledge.stream_privacy_redacted",
                    correlation_id=persisted.correlation_id,
                    actor_type="system",
                    actor_id="privacy-filter",
                    project_id=persisted.project_id,
                    subject_type="interaction",
                    subject_id=str(persisted.id),
                    payload={
                        "findings": len(stream_privacy_findings),
                        "kinds": sorted(kind for kind, _ in stream_privacy_findings),
                        "locations": sorted(location for _, location in stream_privacy_findings),
                    },
                )
        return {
            "interaction_id": str(interaction_id),
            "citations": len(accepted),
            "change_request_id": (
                str(change_request_id) if change_request_id is not None else None
            ),
        }

    async def _draft_heartbeat(
        self,
        interaction: Interaction,
        *,
        policy_guard: Callable[[], Awaitable[None]] | None = None,
        refresh: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        interval = (
            4
            if interaction_delivery_scope(interaction) == "group"
            else PRIVATE_DRAFT_KEEPALIVE_SECONDS
        )
        while True:
            if policy_guard is not None:
                try:
                    await policy_guard()
                except (ClaudeError, ServiceError):
                    return
            try:
                if refresh is not None:
                    if await refresh():
                        return
                else:
                    await self.telegram.send_knowledge_progress(interaction)
            except Exception:
                log.warning(
                    "telegram.draft_failed",
                    interaction_id=str(interaction.id),
                    exc_info=True,
                )
            await asyncio.sleep(interval)

    async def _persist_stream_delivery(
        self,
        interaction: Interaction,
        delivery: dict[str, Any],
    ) -> None:
        source_ref = {**interaction.source_ref, "delivery": dict(delivery)}
        async with self.database.session() as session:
            persisted = await session.get(Interaction, interaction.id)
            if persisted is None:
                raise ServiceError("request_not_found", "Interaction disappeared during streaming")
            persisted.source_ref = source_ref
            await append_audit(
                session,
                event_type="telegram.stream_promoted",
                correlation_id=persisted.correlation_id,
                actor_type="system",
                actor_id="telegram-stream",
                project_id=persisted.project_id,
                subject_type="interaction",
                subject_id=str(persisted.id),
                payload={"delivery_kind": delivery.get("kind")},
            )
        interaction.source_ref = source_ref

    async def _publish_interaction(self, interaction_id: UUID) -> dict[str, Any]:
        policy_blocked = False
        async with self.database.session() as session:
            interaction = await session.get(Interaction, interaction_id)
            if interaction is None:
                raise ServiceError("request_not_found", "Generated answer is unavailable")
            blocked = interaction.error_code == "privacy_blocked"
            if not blocked and interaction.answer_markdown is None:
                raise ServiceError("request_not_found", "Generated answer is unavailable")
            agent_settings = await load_project_agent_settings(session, interaction.project_id)
            if not blocked:
                native_context = (
                    interaction.provider_metadata.get("native_context")
                    if isinstance(interaction.provider_metadata, dict)
                    else None
                )
                expected_policy_hash = (
                    native_context.get("policy_sha256")
                    if isinstance(native_context, dict)
                    else None
                )
                repository = (
                    await session.get(Repository, interaction.repository_id)
                    if interaction.repository_id is not None
                    else None
                )
                live_policy_hash: str | None = None
                try:
                    if repository is not None and agent_settings.enabled:
                        live_profile = await load_live_requester_profile(session, interaction)
                        live_policy_hash = compile_agent_policy(
                            project_settings=agent_settings,
                            requester_profile=live_profile,
                            delivery_scope=interaction_delivery_scope(interaction),
                            repository_allowed_paths=repository.allowed_paths or [],
                            repository_denied_globs=agent_settings.denied_globs or [],
                            agent_role=interaction_agent_role(interaction),
                        ).policy_sha256
                except (ClaudeError, ServiceError):
                    live_policy_hash = None
                if (
                    not isinstance(expected_policy_hash, str)
                    or live_policy_hash != expected_policy_hash
                ):
                    interaction.status = "failed"
                    interaction.error_code = "context_policy_changed"
                    await append_audit(
                        session,
                        event_type="knowledge.answer_publish_policy_blocked",
                        correlation_id=interaction.correlation_id,
                        actor_type="system",
                        actor_id="policy-revalidator",
                        project_id=interaction.project_id,
                        subject_type="interaction",
                        subject_id=str(interaction.id),
                        outcome="blocked",
                        payload={
                            "expected_policy_sha256": expected_policy_hash,
                            "live_policy_sha256": live_policy_hash,
                        },
                    )
                    policy_blocked = True
            session.expunge(interaction)
        if policy_blocked:
            await self.telegram.publish_knowledge_error(interaction)
            return {
                "interaction_id": str(interaction_id),
                "accepted_by_telegram": True,
                "privacy_blocked": False,
                "policy_blocked": True,
            }
        if blocked:
            await self.telegram.publish_knowledge_error(interaction)
        else:
            document_was_requested = (
                isinstance(interaction.provider_metadata, dict)
                and interaction.provider_metadata.get("document_requested") is True
            )
            attach_markdown = agent_settings.telegram_attach_markdown and document_was_requested
            await self.telegram.publish_knowledge_answer(
                interaction,
                interaction.answer_markdown or "",
                artifacts=interaction.artifacts if attach_markdown else [],
                attach_markdown=attach_markdown,
            )
        async with self.database.session() as session:
            persisted = await session.get(Interaction, interaction_id)
            if persisted is None:
                raise ServiceError("request_not_found", "Interaction disappeared")
            if not blocked:
                persisted.status = "published"
            await append_audit(
                session,
                event_type=(
                    "knowledge.answer_privacy_block_notified"
                    if blocked
                    else "knowledge.answer.published"
                ),
                correlation_id=persisted.correlation_id,
                actor_type="system",
                actor_id="telegram-worker",
                project_id=persisted.project_id,
                subject_type="interaction",
                subject_id=str(persisted.id),
                payload={"accepted_by_telegram": True, "privacy_blocked": blocked},
            )
        return {
            "interaction_id": str(interaction_id),
            "accepted_by_telegram": True,
            "privacy_blocked": blocked,
        }

    async def _sync_repository(
        self,
        repository_id: UUID,
        *,
        generation: int = 0,
        requested_commit: str | None = None,
        source: str = "manual",
    ) -> dict[str, Any]:
        async with self.database.session() as session:
            repository = await session.scalar(
                select(Repository).where(Repository.id == repository_id)
            )
            if repository is None:
                raise ServiceError("source_unavailable", "Repository was not found")
            if repository.status == RepositoryStatus.DISABLED.value:
                raise ServiceError("repository_disabled", "Repository synchronization is disabled")
            if generation < repository.sync_generation:
                return await self._mark_repository_sync_superseded(
                    session,
                    repository,
                    generation=generation,
                    source=source,
                    phase="before_lock",
                    mark_stale=False,
                )
            if generation > repository.sync_generation:
                raise ServiceError(
                    "repository_sync_generation_invalid",
                    "Repository sync generation is ahead of persisted state",
                )

        async with self._repository_sync_lock(repository_id):
            async with self.database.session() as session:
                repository = await session.scalar(
                    select(Repository).where(Repository.id == repository_id).with_for_update()
                )
                if repository is None:
                    raise ServiceError("source_unavailable", "Repository was not found")
                if repository.status == RepositoryStatus.DISABLED.value:
                    raise ServiceError(
                        "repository_disabled", "Repository synchronization is disabled"
                    )
                if generation < repository.sync_generation:
                    return await self._mark_repository_sync_superseded(
                        session,
                        repository,
                        generation=generation,
                        source=source,
                        phase="before_fetch",
                        mark_stale=repository.status == RepositoryStatus.SYNCING.value,
                    )
                if generation != repository.sync_generation:
                    raise ServiceError(
                        "repository_sync_generation_invalid",
                        "Repository sync generation is ahead of persisted state",
                    )
                repository.status = RepositoryStatus.SYNCING.value
                await session.flush()
                session.expunge(repository)
            try:
                commit = await self.snapshots.sync(repository)
                await self.snapshots.materialize(repository, commit)
            except ClaudeError as exc:
                async with self.database.session() as session:
                    persisted = await session.scalar(
                        select(Repository).where(Repository.id == repository_id).with_for_update()
                    )
                    if persisted is None:
                        raise ServiceError("source_unavailable", "Repository disappeared") from exc
                    if generation != persisted.sync_generation:
                        return await self._mark_repository_sync_superseded(
                            session,
                            persisted,
                            generation=generation,
                            source=source,
                            phase="fetch_failed",
                            mark_stale=True,
                        )
                    persisted.status = RepositoryStatus.FAILED.value
                    persisted.last_error = exc.message[:2_000]
                raise
            async with self.database.session() as session:
                persisted = await session.scalar(
                    select(Repository).where(Repository.id == repository_id).with_for_update()
                )
                if persisted is None:
                    raise ServiceError("source_unavailable", "Repository disappeared")
                if generation != persisted.sync_generation:
                    return await self._mark_repository_sync_superseded(
                        session,
                        persisted,
                        generation=generation,
                        source=source,
                        phase="after_materialize",
                        mark_stale=True,
                        fetched_commit=commit,
                    )
                persisted.status = RepositoryStatus.READY.value
                persisted.current_commit = commit
                persisted.last_synced_at = utcnow()
                persisted.last_error = None
                await append_audit(
                    session,
                    event_type="repository.synced",
                    correlation_id=f"repository:{repository_id}:{commit}",
                    actor_type="system",
                    actor_id="repository-worker",
                    project_id=persisted.project_id,
                    subject_type="repository",
                    subject_id=str(repository_id),
                    payload={
                        "commit": commit,
                        "generation": generation,
                        "requested_commit": requested_commit,
                        "source": source,
                    },
                )
            return {
                "repository_id": str(repository_id),
                "commit": commit,
                "generation": generation,
            }

    @staticmethod
    async def _mark_repository_sync_superseded(
        session: Any,
        repository: Repository,
        *,
        generation: int,
        source: str,
        phase: str,
        mark_stale: bool,
        fetched_commit: str | None = None,
    ) -> dict[str, Any]:
        if mark_stale and repository.status != RepositoryStatus.DISABLED.value:
            repository.status = RepositoryStatus.STALE.value
        await append_audit(
            session,
            event_type="repository.sync_superseded",
            correlation_id=f"repository:{repository.id}:generation:{generation}",
            actor_type="system",
            actor_id="repository-worker",
            project_id=repository.project_id,
            subject_type="repository",
            subject_id=str(repository.id),
            outcome="superseded",
            payload={
                "generation": generation,
                "current_generation": repository.sync_generation,
                "fetched_commit": fetched_commit,
                "phase": phase,
                "source": source,
            },
        )
        return {
            "repository_id": str(repository.id),
            "generation": generation,
            "current_generation": repository.sync_generation,
            "superseded": True,
        }

    async def _succeed(self, job: Job, result: dict[str, Any]) -> None:
        async with self.database.session() as session:
            await session.execute(
                update(Job)
                .where(Job.id == job.id, Job.status == JobStatus.RUNNING.value)
                .values(
                    status=JobStatus.SUCCEEDED.value,
                    result=result,
                    locked_at=None,
                    locked_by=None,
                    last_error_code=None,
                    last_error_detail=None,
                    updated_at=utcnow(),
                )
            )
        log.info("job.succeeded", job_id=str(job.id), kind=job.kind)

    async def _retry(
        self,
        job: Job,
        code: str,
        detail: str,
        *,
        delay: int | float | None = None,
        consume_attempt: bool = True,
    ) -> bool:
        if consume_attempt and job.attempts >= job.max_attempts:
            await self._fail(job, code, detail)
            return False
        backoff = delay if delay is not None else min(2**job.attempts, 60)
        values: dict[str, Any] = {
            "status": JobStatus.RETRY.value,
            "available_at": utcnow() + timedelta(seconds=backoff),
            "locked_at": None,
            "locked_by": None,
            "last_error_code": code,
            "last_error_detail": detail[:2_000],
            "updated_at": utcnow(),
        }
        if not consume_attempt:
            values["attempts"] = Job.attempts - 1
        async with self.database.session() as session:
            await session.execute(
                update(Job)
                .where(Job.id == job.id, Job.status == JobStatus.RUNNING.value)
                .values(**values)
            )
        log.warning("job.retry", job_id=str(job.id), kind=job.kind, code=code)
        return True

    async def _fail(self, job: Job, code: str, detail: str) -> None:
        async with self.database.session() as session:
            await session.execute(
                update(Job)
                .where(Job.id == job.id)
                .values(
                    status=JobStatus.FAILED.value,
                    locked_at=None,
                    locked_by=None,
                    last_error_code=code,
                    last_error_detail=detail[:2_000],
                    updated_at=utcnow(),
                )
            )
        log.error("job.failed", job_id=str(job.id), kind=job.kind, code=code)

    async def _fail_agent_message(self, job: Job, code: str) -> None:
        value = job.payload.get("agent_message_id")
        if not value:
            return
        async with self.database.session() as session:
            await session.execute(
                update(AgentMessage)
                .where(AgentMessage.id == UUID(value), AgentMessage.status == "queued")
                .values(status="failed", error_code=code, updated_at=utcnow())
            )

    async def _delivery_uncertain(self, job: Job, code: str, detail: str) -> None:
        async with self.database.session() as session:
            await session.execute(
                update(Job)
                .where(Job.id == job.id)
                .values(
                    status=JobStatus.DELIVERY_UNCERTAIN.value,
                    locked_at=None,
                    locked_by=None,
                    last_error_code=code,
                    last_error_detail=detail[:2_000],
                    updated_at=utcnow(),
                )
            )
            if job.kind == "telegram.deliver_agent_message" and job.payload.get("agent_message_id"):
                await session.execute(
                    update(AgentMessage)
                    .where(AgentMessage.id == UUID(job.payload["agent_message_id"]))
                    .values(status="delivery_uncertain", error_code=code, updated_at=utcnow())
                )
            await append_audit(
                session,
                event_type="telegram.delivery_uncertain",
                correlation_id=f"job:{job.id}",
                actor_type="system",
                actor_id="telegram-worker",
                subject_type="job",
                subject_id=str(job.id),
                outcome="uncertain",
                payload={"kind": job.kind, "error_code": code},
            )
        log.error("job.delivery_uncertain", job_id=str(job.id), kind=job.kind)

    async def recover_stale_jobs(self) -> None:
        cutoff = utcnow() - timedelta(minutes=10)
        async with self.database.session() as session:
            stale = list(
                await session.scalars(
                    select(Job).where(
                        Job.status == JobStatus.RUNNING.value,
                        Job.locked_at < cutoff,
                    )
                )
            )
            for job in stale:
                if job.kind in TELEGRAM_EXTERNAL_ACTIONS:
                    job.status = JobStatus.DELIVERY_UNCERTAIN.value
                    job.last_error_code = "worker_restarted_after_external_action"
                    if job.kind == "telegram.deliver_agent_message" and job.payload.get(
                        "agent_message_id"
                    ):
                        await session.execute(
                            update(AgentMessage)
                            .where(AgentMessage.id == UUID(job.payload["agent_message_id"]))
                            .values(
                                status="delivery_uncertain",
                                error_code="worker_restarted_after_external_action",
                                updated_at=utcnow(),
                            )
                        )
                else:
                    job.status = JobStatus.RETRY.value
                    job.available_at = utcnow()
                    job.last_error_code = "worker_restarted"
                job.locked_at = None
                job.locked_by = None
        if stale:
            log.warning("jobs.recovered", count=len(stale))

    async def _sweep_expired_if_due(self) -> None:
        now = asyncio.get_running_loop().time()
        if now - self._last_expiry_sweep < 30:
            return
        self._last_expiry_sweep = now
        async with self.database.session() as session:
            for clarification in await list_expired_pending(session):
                await expire_clarification(session, clarification)

    async def _reconcile_repositories_if_due(self) -> None:
        loop_now = asyncio.get_running_loop().time()
        interval = self.settings.repository_reconcile_seconds
        if loop_now - self._last_repository_reconcile < interval:
            return
        self._last_repository_reconcile = loop_now
        cutoff = utcnow() - timedelta(seconds=interval)
        bucket = int(utcnow().timestamp()) // interval
        queued = 0
        async with self.database.session() as session:
            repositories = list(
                await session.scalars(
                    select(Repository)
                    .where(
                        Repository.auto_sync_enabled.is_(True),
                        Repository.github_repository.is_not(None),
                        Repository.status != RepositoryStatus.DISABLED.value,
                        (
                            Repository.last_synced_at.is_(None)
                            | (Repository.last_synced_at <= cutoff)
                        ),
                    )
                    .order_by(Repository.id)
                    .with_for_update(skip_locked=True)
                )
            )
            for repository in repositories:
                active_job = await session.scalar(
                    select(Job)
                    .where(
                        Job.kind == "repository.sync",
                        Job.status.in_(
                            (
                                JobStatus.QUEUED.value,
                                JobStatus.RUNNING.value,
                                JobStatus.RETRY.value,
                            )
                        ),
                        Job.payload["repository_id"].as_string() == str(repository.id),
                    )
                    .limit(1)
                )
                if active_job is not None:
                    continue
                _, created = await enqueue_repository_sync(
                    session,
                    repository=repository,
                    source="reconcile",
                    deduplication_key=f"repository:{repository.id}:reconcile:{bucket}",
                )
                queued += int(created)
        if queued:
            log.info("repository.reconcile_queued", count=queued)

    async def _publish_interaction_error(self, job: Job, error_code: str) -> None:
        if job.kind != "knowledge.answer":
            return
        interaction_id_value = job.payload.get("interaction_id")
        if not interaction_id_value:
            return
        interaction_id = UUID(interaction_id_value)
        async with self.database.session() as session:
            interaction = await session.get(Interaction, interaction_id)
            if interaction is None:
                return
            interaction.status = "failed"
            interaction.error_code = error_code
            await session.flush()
            session.expunge(interaction)
        try:
            await self.telegram.publish_knowledge_error(interaction)
        except Exception:
            log.warning("telegram.error_publish_failed", interaction_id=str(interaction_id))


def sanitize_knowledge_answer(
    answer: KnowledgeAnswer,
    *,
    level: PrivacyLevel,
) -> tuple[KnowledgeAnswer, list[PrivacyFinding], bool]:
    findings: list[PrivacyFinding] = []
    answer_result = sanitize_agent_output(
        answer.answer_markdown,
        level=level,
        location="answer_markdown",
    )
    findings.extend(answer_result.findings)

    uncertainty: list[str] = []
    blocked = answer_result.blocked
    for index, item in enumerate(answer.uncertainty):
        result = sanitize_agent_output(item, level=level, location=f"uncertainty[{index}]")
        uncertainty.append(result.text)
        findings.extend(result.findings)
        blocked = blocked or result.blocked

    artifacts: list[KnowledgeArtifact] = []
    for artifact in answer.artifacts:
        result = sanitize_agent_output(
            artifact.content,
            level=level,
            location=f"artifact:{artifact.name}",
        )
        artifacts.append(artifact.model_copy(update={"content": result.text}))
        findings.extend(result.findings)
        blocked = blocked or result.blocked

    change_request: AgentChangeRequestProposal | None = None
    if answer.change_request is not None:
        title_result = sanitize_agent_output(
            answer.change_request.title,
            level=level,
            location="change_request.title",
        )
        summary_result = sanitize_agent_output(
            answer.change_request.summary,
            level=level,
            location="change_request.summary",
        )
        findings.extend(title_result.findings)
        findings.extend(summary_result.findings)
        blocked = blocked or title_result.blocked or summary_result.blocked
        change_request = answer.change_request.model_copy(
            update={"title": title_result.text, "summary": summary_result.text}
        )

    return (
        answer.model_copy(
            update={
                "answer_markdown": answer_result.text,
                "uncertainty": uncertainty,
                "artifacts": artifacts,
                "change_request": change_request,
            }
        ),
        findings,
        blocked,
    )


def serialize_artifacts(artifacts: list[KnowledgeArtifact]) -> list[dict[str, Any]]:
    return [
        {
            "name": artifact.name,
            "media_type": "text/markdown",
            "size_bytes": len(artifact.content.encode()),
            "content": artifact.content,
        }
        for artifact in artifacts
    ]


def privacy_audit_payload(findings: list[PrivacyFinding]) -> dict[str, Any]:
    return {
        "findings": len(findings),
        "kinds": sorted({finding["kind"] for finding in findings}),
        "locations": sorted({finding["location"] for finding in findings}),
    }


def render_answer(
    *,
    answer_markdown: str,
    uncertainty: list[str],
) -> str:
    sections = [answer_markdown.rstrip()]
    if uncertainty:
        sections.extend(["## Неопределённость", "\n".join(f"- {item}" for item in uncertainty)])
    return "\n\n".join(sections)


def normalize_guard_reply(value: str) -> str:
    """Keep the generated guard voice while forcing safe plain Telegram formatting."""
    without_list_markers = re.sub(
        r"(?m)^\s{0,3}(?:#{1,6}\s+|[-*•]\s+|\d+[.)]\s+)",
        "",
        value,
    )
    without_markdown = without_list_markers.replace("`", "").replace("**", "")
    return re.sub(r"\s+", " ", without_markdown).strip()


def conversation_message_record(value: str, *, limit: int = 32_000) -> str:
    if len(value) <= limit:
        return value
    marker = "\n\n[Полный ответ сохранён в interaction; память содержит начало и конец.]\n\n"
    head = (limit * 3) // 4
    tail = limit - head - len(marker)
    return f"{value[:head]}{marker}{value[-tail:]}"


async def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    await Worker(settings).run_forever()


def configure_logging(settings: Settings) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ]
    )


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
