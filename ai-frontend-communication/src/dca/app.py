import hashlib
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit
from uuid import UUID

import anyio
import orjson
import redis.asyncio as redis
import structlog
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import ORJSONResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from dca.config import Settings, get_settings
from dca.db import (
    AuditEvent,
    ChangeRequest,
    Clarification,
    Database,
    Job,
    Project,
    Repository,
    TelegramUpdate,
    append_audit,
    enqueue_job,
)
from dca.domain import (
    ChangeRequestStatus,
    ClarificationStatus,
    JobStatus,
    utcnow,
)
from dca.mcp import build_mcp
from dca.service import ServiceError, update_change_request_status
from dca.telegram import TelegramAdapter
from dca.worker import configure_logging

log = structlog.get_logger()
SESSION_COOKIE = "dca_admin"
SESSION_MAX_AGE = 8 * 60 * 60


class LoginInput(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(min_length=1, max_length=1_024)


class AdminIdentity(BaseModel):
    email: str
    role: str = "owner"


class StatusUpdateInput(BaseModel):
    status: ChangeRequestStatus
    expected_version: int = Field(ge=1)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    database = Database(settings)
    telegram = TelegramAdapter(settings, database)
    redis_client: redis.Redis = redis.from_url(  # type: ignore[no-untyped-call]
        settings.redis_url, decode_responses=True
    )
    mcp_server = build_mcp(settings, database)
    mcp_application = mcp_server.streamable_http_app()
    raw_session_secret = settings.session_secret.get_secret_value()
    admin_auth_configured = (
        bool(settings.admin_password_hash.get_secret_value()) and len(raw_session_secret) >= 32
    )
    sessions = URLSafeTimedSerializer(
        raw_session_secret or secrets.token_urlsafe(48),
        salt="dca-admin-session-v1",
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        settings.repository_root.mkdir(parents=True, exist_ok=True)
        settings.snapshot_root.mkdir(parents=True, exist_ok=True)
        async with mcp_server.session_manager.run():
            yield
        await telegram.close()
        await redis_client.aclose()
        await database.close()

    app = FastAPI(
        title="Developer Communication Agent",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.telegram = telegram
    app.state.redis = redis_client

    def require_admin(
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> AdminIdentity:
        if not admin_auth_configured:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "admin_auth_not_configured",
            )
        if not session_cookie:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication_required")
        try:
            payload = sessions.loads(session_cookie, max_age=SESSION_MAX_AGE)
        except (BadSignature, SignatureExpired) as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session_expired") from exc
        if not isinstance(payload, dict) or payload.get("email") != settings.admin_email:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_session")
        return AdminIdentity(email=settings.admin_email)

    def require_same_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        if origin is None:
            return
        expected = urlsplit(str(settings.public_url))
        actual = urlsplit(origin)
        if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "cross_origin_mutation_denied")

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready(deep: bool = False) -> ORJSONResponse:
        checks: dict[str, Any] = {"database": False, "redis": False}
        try:
            async with database.session() as session:
                await session.execute(text("SELECT 1"))
            checks["database"] = True
        except Exception:
            log.warning("readiness.database_failed", exc_info=True)
        try:
            checks["redis"] = bool(await redis_client.ping())
        except Exception:
            log.warning("readiness.redis_failed", exc_info=True)
        if deep and settings.telegram_bot_token.get_secret_value():
            try:
                bot = await telegram.bot.get_me()
                checks["telegram"] = {
                    "ok": True,
                    "has_topics_enabled": bot.has_topics_enabled,
                    "supports_guest_queries": bot.supports_guest_queries,
                }
            except Exception:
                checks["telegram"] = {"ok": False}
        ready_now = bool(checks["database"])
        return ORJSONResponse(
            status_code=status.HTTP_200_OK if ready_now else status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "ok" if ready_now else "not_ready", "checks": checks},
        )

    @app.post("/api/v1/auth/login")
    async def login(payload: LoginInput, request: Request, response: Response) -> AdminIdentity:
        if not admin_auth_configured:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "admin_auth_not_configured",
            )
        await enforce_login_rate_limit(request, redis_client, settings)
        expected_hash = settings.admin_password_hash.get_secret_value()
        valid = payload.email.casefold() == settings.admin_email.casefold() and bool(expected_hash)
        if valid:
            try:
                valid = await anyio.to_thread.run_sync(
                    PasswordHasher().verify,
                    expected_hash,
                    payload.password,
                )
            except (InvalidHashError, VerificationError):
                valid = False
        if not valid:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_credentials")
        token = sessions.dumps({"email": settings.admin_email, "issued_at": utcnow().isoformat()})
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            path="/",
        )
        return AdminIdentity(email=settings.admin_email)

    @app.get("/api/v1/auth/me")
    async def me(admin: Annotated[AdminIdentity, Depends(require_admin)]) -> AdminIdentity:
        return admin

    @app.post("/api/v1/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(
        response: Response,
        _: Annotated[AdminIdentity, Depends(require_admin)],
        __: Annotated[None, Depends(require_same_origin)],
    ) -> None:
        response.delete_cookie(SESSION_COOKIE, path="/")

    @app.post("/webhooks/telegram", include_in_schema=False)
    async def telegram_webhook(
        request: Request,
        x_telegram_bot_api_secret_token: Annotated[
            str | None, Header(alias="X-Telegram-Bot-Api-Secret-Token")
        ] = None,
    ) -> dict[str, bool]:
        expected_secret = settings.telegram_webhook_secret.get_secret_value()
        if (
            not expected_secret
            or not x_telegram_bot_api_secret_token
            or not secrets.compare_digest(expected_secret, x_telegram_bot_api_secret_token)
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_webhook_secret")
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > settings.max_telegram_body_bytes:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "update_too_large")
        body = await request.body()
        if len(body) > settings.max_telegram_body_bytes:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "update_too_large")
        try:
            payload = orjson.loads(body)
            update_id = int(payload["update_id"])
        except (KeyError, TypeError, ValueError, orjson.JSONDecodeError) as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_update") from exc

        async with database.session() as session:
            inserted = await reserve_telegram_update(session, update_id, payload)
            if not inserted:
                return {"ok": True}
            if payload.get("guest_message") is not None:
                try:
                    inline_message_id = await telegram.answer_guest_placeholder(payload)
                except Exception as exc:
                    await mark_guest_uncertain(session, update_id, type(exc).__name__)
                    return {"ok": True}
                if inline_message_id is not None:
                    payload["_dca_inline_message_id"] = inline_message_id
            await queue_telegram_update(session, update_id, payload)
        return {"ok": True}

    @app.get("/api/v1/projects")
    async def list_projects(
        _: Annotated[AdminIdentity, Depends(require_admin)],
    ) -> list[dict[str, Any]]:
        async with database.session() as session:
            projects = list(await session.scalars(select(Project).order_by(Project.name)))
            return [
                {
                    "id": str(project.id),
                    "slug": project.slug,
                    "name": project.name,
                    "enabled": project.enabled,
                }
                for project in projects
            ]

    @app.get("/api/v1/overview")
    async def overview(
        _: Annotated[AdminIdentity, Depends(require_admin)],
        project_id: UUID | None = None,
    ) -> dict[str, Any]:
        async with database.session() as session:
            request_query = (
                select(func.count())
                .select_from(ChangeRequest)
                .where(ChangeRequest.status.in_(["open", "in_progress"]))
            )
            clarification_query = (
                select(func.count())
                .select_from(Clarification)
                .where(Clarification.status == ClarificationStatus.PENDING.value)
            )
            repository_query = (
                select(func.count()).select_from(Repository).where(Repository.status == "failed")
            )
            recent_query = select(AuditEvent).order_by(AuditEvent.occurred_at.desc()).limit(20)
            if project_id:
                request_query = request_query.where(ChangeRequest.project_id == project_id)
                clarification_query = clarification_query.where(
                    Clarification.project_id == project_id
                )
                repository_query = repository_query.where(Repository.project_id == project_id)
                recent_query = recent_query.where(AuditEvent.project_id == project_id)

            open_requests = await session.scalar(request_query)
            pending_clarifications = await session.scalar(clarification_query)
            repository_errors = await session.scalar(repository_query)
            delivery_uncertain = await session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.status == JobStatus.DELIVERY_UNCERTAIN.value)
            )
            recent = list(await session.scalars(recent_query))
        return {
            "attention": {
                "open_requests": open_requests or 0,
                "pending_clarifications": pending_clarifications or 0,
                "repository_errors": repository_errors or 0,
                "delivery_uncertain": delivery_uncertain or 0,
            },
            "recent_events": [serialize_audit(event) for event in recent],
        }

    @app.get("/api/v1/clarifications")
    async def list_clarifications(
        _: Annotated[AdminIdentity, Depends(require_admin)],
        project_id: UUID | None = None,
        clarification_status: Annotated[ClarificationStatus | None, Query(alias="status")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, Any]]:
        query = select(Clarification).order_by(Clarification.created_at.desc())
        if project_id:
            query = query.where(Clarification.project_id == project_id)
        if clarification_status:
            query = query.where(Clarification.status == clarification_status.value)
        async with database.session() as session:
            rows = list(await session.scalars(query.limit(limit).offset(offset)))
        return [serialize_clarification(row) for row in rows]

    @app.get("/api/v1/clarifications/{request_id}")
    async def clarification_detail(
        request_id: UUID,
        _: Annotated[AdminIdentity, Depends(require_admin)],
    ) -> dict[str, Any]:
        async with database.session() as session:
            row = await session.get(Clarification, request_id)
            if row is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "clarification_not_found")
            events = list(
                await session.scalars(
                    select(AuditEvent)
                    .where(
                        AuditEvent.subject_type == "clarification",
                        AuditEvent.subject_id == str(request_id),
                    )
                    .order_by(AuditEvent.occurred_at)
                )
            )
        return {**serialize_clarification(row), "events": [serialize_audit(e) for e in events]}

    @app.get("/api/v1/requests")
    async def list_requests(
        _: Annotated[AdminIdentity, Depends(require_admin)],
        project_id: UUID | None = None,
        request_status: Annotated[ChangeRequestStatus | None, Query(alias="status")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, Any]]:
        query = select(ChangeRequest).order_by(ChangeRequest.created_at.desc())
        if project_id:
            query = query.where(ChangeRequest.project_id == project_id)
        if request_status:
            query = query.where(ChangeRequest.status == request_status.value)
        async with database.session() as session:
            rows = list(await session.scalars(query.limit(limit).offset(offset)))
        return [serialize_request(row) for row in rows]

    @app.patch("/api/v1/requests/{request_id}/status")
    async def change_request_status(
        request_id: UUID,
        payload: StatusUpdateInput,
        admin: Annotated[AdminIdentity, Depends(require_admin)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> dict[str, Any]:
        try:
            async with database.session() as session:
                row = await update_change_request_status(
                    session,
                    request_id=request_id,
                    target=payload.status,
                    expected_version=payload.expected_version,
                    actor_id=admin.email,
                )
                return serialize_request(row)
        except ServiceError as exc:
            raise service_http_error(exc) from exc

    @app.get("/api/v1/repositories")
    async def list_repositories(
        _: Annotated[AdminIdentity, Depends(require_admin)],
    ) -> list[dict[str, Any]]:
        async with database.session() as session:
            rows = list(await session.scalars(select(Repository).order_by(Repository.name)))
        return [serialize_repository(row) for row in rows]

    @app.post("/api/v1/repositories/{repository_id}/sync", status_code=status.HTTP_202_ACCEPTED)
    async def sync_repository(
        repository_id: UUID,
        admin: Annotated[AdminIdentity, Depends(require_admin)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> dict[str, Any]:
        async with database.session() as session:
            repository = await session.scalar(
                select(Repository).where(Repository.id == repository_id).with_for_update()
            )
            if repository is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "repository_not_found")
            job = await session.scalar(
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
                    Job.payload["repository_id"].as_string() == str(repository_id),
                )
                .order_by(Job.created_at)
                .limit(1)
            )
            if job is not None:
                return {"job_id": str(job.id), "status": job.status}
            job = await enqueue_job(
                session,
                kind="repository.sync",
                payload={"repository_id": str(repository_id)},
                deduplication_key=f"repository:{repository_id}:sync:{secrets.token_hex(8)}",
                max_attempts=3,
            )
            await append_audit(
                session,
                event_type="repository.sync_requested",
                correlation_id=f"repository:{repository_id}:sync:{job.id}",
                actor_type="admin",
                actor_id=admin.email,
                project_id=repository.project_id,
                subject_type="repository",
                subject_id=str(repository.id),
            )
        return {"job_id": str(job.id), "status": job.status}

    @app.get("/api/v1/audit")
    async def list_audit(
        _: Annotated[AdminIdentity, Depends(require_admin)],
        project_id: UUID | None = None,
        correlation_id: str | None = None,
        event_type: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, Any]]:
        query = select(AuditEvent).order_by(AuditEvent.occurred_at.desc())
        if project_id:
            query = query.where(AuditEvent.project_id == project_id)
        if correlation_id:
            query = query.where(AuditEvent.correlation_id == correlation_id)
        if event_type:
            query = query.where(AuditEvent.event_type == event_type)
        async with database.session() as session:
            rows = list(await session.scalars(query.limit(limit).offset(offset)))
        return [serialize_audit(row) for row in rows]

    frontend = Path("web/dist")
    if frontend.is_dir():
        app.mount("/admin", StaticFiles(directory=frontend, html=True), name="admin")
    # Mount last: FastMCP owns /mcp and the RFC 9728 metadata URL at the public root.
    app.mount("/", mcp_application)
    return app


async def reserve_telegram_update(
    session: AsyncSession,
    update_id: int,
    payload: dict[str, Any],
) -> bool:
    update_type = next((key for key in payload if key != "update_id"), "unknown")
    result = await session.execute(
        insert(TelegramUpdate)
        .values(update_id=update_id, update_type=update_type, payload=payload)
        .on_conflict_do_nothing(index_elements=[TelegramUpdate.update_id])
        .returning(TelegramUpdate.update_id)
    )
    return result.scalar_one_or_none() is not None


async def queue_telegram_update(
    session: AsyncSession,
    update_id: int,
    payload: dict[str, Any],
) -> None:
    await session.execute(
        update(TelegramUpdate).where(TelegramUpdate.update_id == update_id).values(payload=payload)
    )
    await enqueue_job(
        session,
        kind="telegram.process_update",
        payload={"update_id": update_id},
        deduplication_key=f"telegram-update:{update_id}",
    )


async def mark_guest_uncertain(session: AsyncSession, update_id: int, error_code: str) -> None:
    row = await session.get(TelegramUpdate, update_id)
    if row is None:
        return
    payload = dict(row.payload)
    payload["_dca_guest_answer_status"] = "uncertain"
    row.payload = payload
    await append_audit(
        session,
        event_type="telegram.guest_answer_uncertain",
        correlation_id=f"telegram-update:{update_id}",
        actor_type="system",
        actor_id="webhook",
        outcome="uncertain",
        subject_type="telegram_update",
        subject_id=str(update_id),
        payload={"error_code": error_code},
    )


async def enforce_login_rate_limit(
    request: Request,
    client: redis.Redis,
    settings: Settings,
) -> None:
    address = request.client.host if request.client else "unknown"
    pepper = settings.session_secret.get_secret_value().encode()
    digest = hashlib.sha256(pepper + address.encode()).hexdigest()[:24]
    key = f"dca:login:{digest}"
    try:
        count = await client.incr(key)
        if count == 1:
            await client.expire(key, 300)
    except Exception:
        log.warning("login.rate_limit_unavailable")
        return
    if count > 10:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited")


def serialize_clarification(row: Clarification) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "recipient_user_id": str(row.recipient_user_id),
        "agent_run_id": row.agent_run_id,
        "correlation_id": row.correlation_id,
        "context": row.context,
        "question": row.question,
        "expected_answer": row.expected_answer,
        "status": row.status,
        "expires_at": row.expires_at,
        "answer": row.answer_raw,
        "answered_at": row.answered_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def serialize_request(row: ChangeRequest) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "correlation_id": row.correlation_id,
        "source": row.source,
        "kind": row.kind,
        "title": row.title,
        "description": row.description,
        "priority": row.priority,
        "status": row.status,
        "version": row.version,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def serialize_repository(row: Repository) -> dict[str, Any]:
    safe_url = row.ssh_url.split("@", 1)[-1]
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "name": row.name,
        "ssh_url": safe_url,
        "default_branch": row.default_branch,
        "allowed_paths": row.allowed_paths,
        "current_commit": row.current_commit,
        "status": row.status,
        "last_synced_at": row.last_synced_at,
        "last_error": row.last_error,
    }


def serialize_audit(row: AuditEvent) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "event_type": row.event_type,
        "event_version": row.event_version,
        "occurred_at": row.occurred_at,
        "project_id": str(row.project_id) if row.project_id else None,
        "actor": {"type": row.actor_type, "id": row.actor_id},
        "correlation_id": row.correlation_id,
        "subject": {"type": row.subject_type, "id": row.subject_id},
        "outcome": row.outcome,
        "payload": row.payload,
    }


def service_http_error(error: ServiceError) -> HTTPException:
    status_code = {
        "request_not_found": status.HTTP_404_NOT_FOUND,
        "version_conflict": status.HTTP_409_CONFLICT,
        "invalid_state_transition": status.HTTP_409_CONFLICT,
        "forbidden": status.HTTP_403_FORBIDDEN,
    }.get(error.code, status.HTTP_400_BAD_REQUEST)
    return HTTPException(status_code, error.as_dict())


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run(
        "dca.app:app",
        host="0.0.0.0",  # noqa: S104 - container entrypoint
        port=8000,
        proxy_headers=True,
    )


if __name__ == "__main__":
    run()
