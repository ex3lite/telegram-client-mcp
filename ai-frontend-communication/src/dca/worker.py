from __future__ import annotations

import asyncio
import os
import random
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from typing import Any
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
    Database,
    Interaction,
    Job,
    Repository,
    TelegramUpdate,
    append_audit,
    enqueue_job,
)
from dca.domain import JobStatus, utcnow
from dca.service import ServiceError, expire_clarification, list_expired_pending
from dca.telegram import TelegramAdapter, ingest_telegram_update, new_draft_id

log = structlog.get_logger()
TELEGRAM_EXTERNAL_ACTIONS = {
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

    async def _answer_interaction(self, interaction_id: UUID) -> dict[str, Any]:
        async with self.database.session() as session:
            interaction = await session.get(Interaction, interaction_id)
            if interaction is None or interaction.repository_id is None:
                raise ServiceError("request_not_found", "Interaction or repository is missing")
            repository = await session.get(Repository, interaction.repository_id)
            if repository is None or interaction.commit_sha is None:
                raise ServiceError("source_unavailable", "Repository snapshot is unavailable")
            interaction.status = "generating"
            await session.flush()
            session.expunge(interaction)
            session.expunge(repository)

        snapshot = await self.snapshots.materialize(repository, interaction.commit_sha)
        heartbeat = asyncio.create_task(self._draft_heartbeat(interaction))
        try:
            result = await self.claude.answer(
                snapshot=snapshot,
                question=interaction.question,
                requester_profile=trusted_requester_profile(interaction),
            )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

        accepted = [check.citation.model_dump(mode="json") for check in result.accepted_citations]
        rejected = [check.model_dump(mode="json") for check in result.rejected_citations]
        rendered = render_answer(
            answer_markdown=result.answer.answer_markdown,
            citations=accepted,
            commit_sha=interaction.commit_sha,
            uncertainty=result.answer.uncertainty,
        )
        async with self.database.session() as session:
            persisted = await session.get(Interaction, interaction_id)
            if persisted is None:
                raise ServiceError("request_not_found", "Interaction disappeared")
            persisted.status = "answer_ready"
            persisted.answer_markdown = rendered
            persisted.citations = accepted
            persisted.rejected_citations = rejected
            persisted.uncertainty = result.answer.uncertainty
            persisted.provider_metadata = {
                "provider": "claude-code-cli",
                "cli_version": result.cli_version,
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
            if interaction is None or interaction.answer_markdown is None:
                raise ServiceError("request_not_found", "Generated answer is unavailable")
            session.expunge(interaction)
        await self.telegram.publish_knowledge_answer(interaction, interaction.answer_markdown)
        async with self.database.session() as session:
            persisted = await session.get(Interaction, interaction_id)
            if persisted is None:
                raise ServiceError("request_not_found", "Interaction disappeared")
            persisted.status = "published"
            await append_audit(
                session,
                event_type="knowledge.answer.published",
                correlation_id=persisted.correlation_id,
                actor_type="system",
                actor_id="telegram-worker",
                project_id=persisted.project_id,
                subject_type="interaction",
                subject_id=str(persisted.id),
                payload={"accepted_by_telegram": True},
            )
        return {"interaction_id": str(interaction_id), "accepted_by_telegram": True}

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
