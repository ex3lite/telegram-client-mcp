from __future__ import annotations

import asyncio
import os
import random
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from typing import Any, cast
from uuid import UUID

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

from dca.claude import ClaudeCode, ClaudeError, RepositorySnapshots
from dca.config import Settings, get_settings
from dca.db import (
    AgentMessage,
    Database,
    Interaction,
    Job,
    ProjectMembership,
    Repository,
    TelegramChat,
    TelegramIdentity,
    TelegramUpdate,
    append_audit,
    enqueue_job,
)
from dca.domain import JobStatus, KnowledgeAnswer, KnowledgeArtifact, utcnow
from dca.privacy import PrivacyFinding, PrivacyLevel, sanitize_text
from dca.service import (
    SYSTEM_SECRET_CLAUDE_OAUTH,
    ServiceError,
    expire_clarification,
    list_expired_pending,
    load_project_agent_settings,
    load_system_secret,
)
from dca.telegram import TelegramAdapter, ingest_telegram_update, new_draft_id

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


def _poll_retry_delay(base_delay: float) -> float:
    return base_delay + random.uniform(0, base_delay * 0.2)  # noqa: S311 - retry jitter


def trusted_requester_profile(interaction: Interaction) -> dict[str, str] | None:
    raw_profile = interaction.source_ref.get("requester_profile")
    if interaction.source != "telegram" or not isinstance(raw_profile, dict):
        return None
    profile = {
        key: value
        for key in ("display_name", "role", "department", "stack")
        if isinstance((value := raw_profile.get(key)), str)
    }
    return profile or None


class Worker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.database = Database(settings)
        self.telegram = TelegramAdapter(settings, self.database)
        self.snapshots = RepositorySnapshots(settings)
        self.claude = ClaudeCode(settings)
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"
        self._last_expiry_sweep = 0.0

    async def run_forever(self) -> None:
        await self.recover_stale_jobs()
        log.info(
            "worker.started",
            worker_id=self.worker_id,
            telegram_mode=self.settings.telegram_mode,
        )
        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self._run_job_loop())
                if self.settings.telegram_mode == "polling":
                    tasks.create_task(self._poll_telegram_forever())
        finally:
            await self.telegram.close()
            await self.database.close()

    async def _run_job_loop(self) -> None:
        while True:
            await self._sweep_expired_if_due()
            job = await self.claim_job()
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
                    if released is not True:
                        log.error("telegram.poll_lock_release_failed")
                except Exception:
                    log.exception("telegram.poll_lock_release_failed")

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

    async def claim_job(self) -> Job | None:
        async with self.database.session() as session:
            job = await session.scalar(
                select(Job)
                .where(
                    Job.status.in_([JobStatus.QUEUED.value, JobStatus.RETRY.value]),
                    Job.available_at <= utcnow(),
                )
                .order_by(Job.available_at, Job.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
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
                    await self._publish_interaction_error(job, exc.message)
            else:
                await self._fail(job, exc.code, exc.message)
                await self._publish_interaction_error(job, exc.message)
        except ServiceError as exc:
            if exc.retryable:
                await self._retry(job, exc.code, exc.message)
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
        if job.kind == "knowledge.answer":
            interaction_id = UUID(job.payload["interaction_id"])
            return await self._answer_interaction(interaction_id)
        if job.kind == "telegram.publish_interaction":
            interaction_id = UUID(job.payload["interaction_id"])
            return await self._publish_interaction(interaction_id)
        if job.kind == "repository.sync":
            repository_id = UUID(job.payload["repository_id"])
            return await self._sync_repository(repository_id)
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
            text_result = sanitize_text(
                message.text_markdown,
                level=level,
                location="agent_message.text_markdown",
            )
            attachment_result = (
                sanitize_text(
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
        return {
            "agent_message_id": str(agent_message_id),
            "status": "sent",
            "telegram_message_id": telegram_message_id,
        }

    async def _answer_interaction(self, interaction_id: UUID) -> dict[str, Any]:
        async with self.database.session() as session:
            interaction = await session.get(Interaction, interaction_id)
            if interaction is None or interaction.repository_id is None:
                raise ServiceError("request_not_found", "Interaction or repository is missing")
            repository = await session.get(Repository, interaction.repository_id)
            if repository is None or interaction.commit_sha is None:
                raise ServiceError("source_unavailable", "Repository snapshot is unavailable")
            agent_settings = await load_project_agent_settings(session, interaction.project_id)
            if not agent_settings.enabled:
                raise ServiceError("agent_disabled", "Agent is disabled for this project")
            oauth_token = await load_system_secret(
                session,
                SYSTEM_SECRET_CLAUDE_OAUTH,
                self.settings.session_secret.get_secret_value(),
            )
            interaction.status = "generating"
            await session.flush()
            session.expunge(interaction)
            session.expunge(repository)

        snapshot = await self.snapshots.materialize(
            repository,
            interaction.commit_sha,
            denied_globs=agent_settings.denied_globs,
        )
        heartbeat = asyncio.create_task(self._draft_heartbeat(interaction))
        try:
            result = await self.claude.answer(
                snapshot=snapshot,
                question=interaction.question,
                project_settings=agent_settings,
                requester_profile=trusted_requester_profile(interaction),
                oauth_token=oauth_token,
            )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

        if agent_settings.privacy_level not in {"strict", "balanced"}:
            raise ServiceError("privacy_policy_invalid", "Project privacy policy is invalid")
        safe_answer, privacy_findings, privacy_blocked = sanitize_knowledge_answer(
            result.answer,
            level=cast(PrivacyLevel, agent_settings.privacy_level),
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
        rendered = render_answer(
            answer_markdown=safe_answer.answer_markdown,
            citations=accepted,
            commit_sha=interaction.commit_sha,
            uncertainty=safe_answer.uncertainty,
        )
        async with self.database.session() as session:
            persisted = await session.get(Interaction, interaction_id)
            if persisted is None:
                raise ServiceError("request_not_found", "Interaction disappeared")
            persisted.status = "answer_ready"
            persisted.answer_markdown = rendered
            persisted.artifacts = serialize_artifacts(safe_answer.artifacts)
            persisted.privacy_findings = [dict(finding) for finding in privacy_findings]
            persisted.citations = accepted
            persisted.rejected_citations = rejected
            persisted.uncertainty = safe_answer.uncertainty
            persisted.provider_metadata = {
                "provider": "claude-code-cli",
                "cli_version": result.cli_version,
                "model": agent_settings.claude_model,
                "effort": agent_settings.claude_effort,
            }
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
        return {"interaction_id": str(interaction_id), "citations": len(accepted)}

    async def _draft_heartbeat(self, interaction: Interaction) -> None:
        draft_id = new_draft_id()
        while True:
            try:
                await self.telegram.send_knowledge_progress(interaction, draft_id)
            except Exception:
                log.warning(
                    "telegram.draft_failed",
                    interaction_id=str(interaction.id),
                    exc_info=True,
                )
            await asyncio.sleep(20)

    async def _publish_interaction(self, interaction_id: UUID) -> dict[str, Any]:
        async with self.database.session() as session:
            interaction = await session.get(Interaction, interaction_id)
            if interaction is None:
                raise ServiceError("request_not_found", "Generated answer is unavailable")
            blocked = interaction.error_code == "privacy_blocked"
            if not blocked and interaction.answer_markdown is None:
                raise ServiceError("request_not_found", "Generated answer is unavailable")
            agent_settings = await load_project_agent_settings(session, interaction.project_id)
            session.expunge(interaction)
        if blocked:
            await self.telegram.publish_knowledge_error(interaction)
        else:
            await self.telegram.publish_knowledge_answer(
                interaction,
                interaction.answer_markdown or "",
                artifacts=interaction.artifacts,
                attach_markdown=agent_settings.telegram_attach_markdown,
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

    async def _sync_repository(self, repository_id: UUID) -> dict[str, Any]:
        async with self.database.session() as session:
            repository = await session.get(Repository, repository_id)
            if repository is None:
                raise ServiceError("source_unavailable", "Repository was not found")
            repository.status = "syncing"
            await session.flush()
            session.expunge(repository)
        try:
            commit = await self.snapshots.sync(repository)
            await self.snapshots.materialize(repository, commit)
        except ClaudeError as exc:
            async with self.database.session() as session:
                persisted = await session.get(Repository, repository_id)
                if persisted is not None:
                    persisted.status = "failed"
                    persisted.last_error = exc.message[:2_000]
            raise
        async with self.database.session() as session:
            persisted = await session.get(Repository, repository_id)
            if persisted is None:
                raise ServiceError("source_unavailable", "Repository disappeared")
            persisted.status = "ready"
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
                payload={"commit": commit},
            )
        return {"repository_id": str(repository_id), "commit": commit}

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
    ) -> bool:
        if job.attempts >= job.max_attempts:
            await self._fail(job, code, detail)
            return False
        backoff = delay if delay is not None else min(2**job.attempts, 60)
        async with self.database.session() as session:
            await session.execute(
                update(Job)
                .where(Job.id == job.id, Job.status == JobStatus.RUNNING.value)
                .values(
                    status=JobStatus.RETRY.value,
                    available_at=utcnow() + timedelta(seconds=backoff),
                    locked_at=None,
                    locked_by=None,
                    last_error_code=code,
                    last_error_detail=detail[:2_000],
                    updated_at=utcnow(),
                )
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

    async def _publish_interaction_error(self, job: Job, _message: str) -> None:
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
            interaction.error_code = "answer_failed"
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
    answer_result = sanitize_text(answer.answer_markdown, level=level, location="answer_markdown")
    findings.extend(answer_result.findings)

    uncertainty: list[str] = []
    blocked = answer_result.blocked
    for index, item in enumerate(answer.uncertainty):
        result = sanitize_text(item, level=level, location=f"uncertainty[{index}]")
        uncertainty.append(result.text)
        findings.extend(result.findings)
        blocked = blocked or result.blocked

    artifacts: list[KnowledgeArtifact] = []
    for artifact in answer.artifacts:
        result = sanitize_text(
            artifact.content,
            level=level,
            location=f"artifact:{artifact.name}",
        )
        artifacts.append(artifact.model_copy(update={"content": result.text}))
        findings.extend(result.findings)
        blocked = blocked or result.blocked

    return (
        answer.model_copy(
            update={
                "answer_markdown": answer_result.text,
                "uncertainty": uncertainty,
                "artifacts": artifacts,
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
    citations: list[dict[str, Any]],
    commit_sha: str,
    uncertainty: list[str],
) -> str:
    source_lines = [
        f"- `{citation['path']}:{citation['start_line']}-{citation['end_line']}` @ `{commit_sha}`"
        for citation in citations
    ]
    sections = [answer_markdown.rstrip(), "## Источники", "\n".join(source_lines)]
    if uncertainty:
        sections.extend(["## Неопределённость", "\n".join(f"- {item}" for item in uncertainty)])
    return "\n\n".join(sections)


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
    asyncio.run(main())


if __name__ == "__main__":
    run()
