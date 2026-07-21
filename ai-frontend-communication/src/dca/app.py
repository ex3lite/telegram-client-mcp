import hashlib
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import orjson
import redis.asyncio as redis
import structlog
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
from fastapi.responses import FileResponse, ORJSONResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, text

from dca.config import Settings, get_settings
from dca.db import (
    AdminAccessKey,
    AdminPrincipal,
    AdminSession,
    AuditEvent,
    ChangeRequest,
    Clarification,
    Database,
    Job,
    Project,
    Repository,
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
from dca.service import (
    ServiceError,
    admin_key_fingerprint,
    update_change_request_status,
    validate_admin_access_key,
)
from dca.telegram import TelegramAdapter, ingest_telegram_update
from dca.worker import configure_logging

log = structlog.get_logger()
SESSION_COOKIE = "dca_admin"
SESSION_MAX_AGE = 180 * 24 * 60 * 60


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_key: UUID

    @field_validator("access_key")
    @classmethod
    def require_uuid4(cls, value: UUID) -> UUID:
        return validate_admin_access_key(value)


class AdminIdentity(BaseModel):
    principal_id: UUID
    name: str
    role: str = "owner"


@dataclass(frozen=True, slots=True)
class AdminContext:
    principal_id: UUID
    session_id: UUID
    name: str

    def identity(self) -> AdminIdentity:
        return AdminIdentity(principal_id=self.principal_id, name=self.name)


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
    admin_auth_configured = len(raw_session_secret) >= 32
    sessions = URLSafeTimedSerializer(
        raw_session_secret or secrets.token_urlsafe(48),
        salt="dca-admin-session-v2",
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

    async def require_admin(
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> AdminContext:
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
        if not isinstance(payload, dict):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_session")
        try:
            session_id = UUID(str(payload["session_id"]))
        except (KeyError, ValueError) as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_session") from exc
        async with database.session() as session:
            admin_session = await session.get(AdminSession, session_id)
            if admin_session is None or admin_session.revoked_at is not None:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session_revoked")
            if admin_session.expires_at <= utcnow():
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session_expired")
            key = await session.get(AdminAccessKey, admin_session.access_key_id)
            if key is None or not key.active:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session_revoked")
            principal = await session.get(AdminPrincipal, key.principal_id)
            if principal is None or not principal.active:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session_revoked")
        return AdminContext(
            principal_id=principal.id,
            session_id=admin_session.id,
            name=principal.name,
        )

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
                    "mode": settings.telegram_mode,
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
        fingerprint = admin_key_fingerprint(payload.access_key, raw_session_secret)
        async with database.session() as session:
            key = await session.scalar(
                select(AdminAccessKey).where(
                    AdminAccessKey.fingerprint == fingerprint,
                    AdminAccessKey.active.is_(True),
                )
            )
            if key is None:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_credentials")
            principal = await session.get(AdminPrincipal, key.principal_id)
            if principal is None or not principal.active:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_credentials")
            now = utcnow()
            key.last_used_at = now
            identity = AdminIdentity(principal_id=principal.id, name=principal.name)
            admin_session = AdminSession(
                id=uuid4(),
                access_key_id=key.id,
                expires_at=now + timedelta(seconds=SESSION_MAX_AGE),
            )
            session.add(admin_session)
            await append_audit(
                session,
                event_type="admin.login_succeeded",
                correlation_id=f"admin-login:{key.id}:{secrets.token_hex(8)}",
                actor_type="admin",
                actor_id=str(principal.id),
                subject_type="admin_access_key",
                subject_id=str(key.id),
            )
        token = sessions.dumps({"session_id": str(admin_session.id)})
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            path="/",
        )
        return identity

    @app.get("/api/v1/auth/me")
    async def me(admin: Annotated[AdminContext, Depends(require_admin)]) -> AdminIdentity:
        return admin.identity()

    @app.post("/api/v1/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(
        response: Response,
        __: Annotated[None, Depends(require_same_origin)],
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> None:
        session_id: UUID | None = None
        if session_cookie:
            try:
                payload = sessions.loads(session_cookie, max_age=SESSION_MAX_AGE)
                if isinstance(payload, dict):
                    session_id = UUID(str(payload["session_id"]))
            except (BadSignature, KeyError, SignatureExpired, ValueError):
                pass
        if session_id is not None:
            async with database.session() as session:
                admin_session = await session.get(AdminSession, session_id)
                if admin_session is not None and admin_session.revoked_at is None:
                    admin_session.revoked_at = utcnow()
                    key = await session.get(AdminAccessKey, admin_session.access_key_id)
                    await append_audit(
                        session,
                        event_type="admin.logout",
                        correlation_id=f"admin-logout:{session_id}:{secrets.token_hex(8)}",
                        actor_type="admin",
                        actor_id=str(key.principal_id) if key is not None else "unknown",
                        subject_type="admin_session",
                        subject_id=str(session_id),
                    )
        response.delete_cookie(
            SESSION_COOKIE,
            path="/",
            secure=settings.cookie_secure,
            httponly=True,
            samesite="lax",
        )

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
            payload["update_id"] = int(payload["update_id"])
        except (KeyError, TypeError, ValueError, orjson.JSONDecodeError) as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_update") from exc

        async with database.session() as session:
            await ingest_telegram_update(session, telegram, payload, actor_id="webhook")
        return {"ok": True}

    @app.get("/api/v1/projects")
    async def list_projects(
        _: Annotated[AdminContext, Depends(require_admin)],
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
        _: Annotated[AdminContext, Depends(require_admin)],
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
        _: Annotated[AdminContext, Depends(require_admin)],
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
        _: Annotated[AdminContext, Depends(require_admin)],
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
        _: Annotated[AdminContext, Depends(require_admin)],
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
        admin: Annotated[AdminContext, Depends(require_admin)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> dict[str, Any]:
        try:
            async with database.session() as session:
                row = await update_change_request_status(
                    session,
                    request_id=request_id,
                    target=payload.status,
                    expected_version=payload.expected_version,
                    actor_id=str(admin.principal_id),
                )
                return serialize_request(row)
        except ServiceError as exc:
            raise service_http_error(exc) from exc

    @app.get("/api/v1/repositories")
    async def list_repositories(
        _: Annotated[AdminContext, Depends(require_admin)],
    ) -> list[dict[str, Any]]:
        async with database.session() as session:
            rows = list(await session.scalars(select(Repository).order_by(Repository.name)))
        return [serialize_repository(row) for row in rows]

    @app.post("/api/v1/repositories/{repository_id}/sync", status_code=status.HTTP_202_ACCEPTED)
    async def sync_repository(
        repository_id: UUID,
        admin: Annotated[AdminContext, Depends(require_admin)],
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
                actor_id=str(admin.principal_id),
                project_id=repository.project_id,
                subject_type="repository",
                subject_id=str(repository.id),
            )
        return {"job_id": str(job.id), "status": job.status}

    @app.get("/api/v1/audit")
    async def list_audit(
        _: Annotated[AdminContext, Depends(require_admin)],
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
        app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="admin-assets")

        @app.get("/", include_in_schema=False)
        async def admin_index() -> FileResponse:
            return FileResponse(frontend / "index.html")

    # Mount last so API, the admin index and assets retain their exact routes.
    app.mount("/", mcp_application)
    return app


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
