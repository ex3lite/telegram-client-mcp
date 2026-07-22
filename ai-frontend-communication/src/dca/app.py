import asyncio
import hashlib
import os
import re
import secrets
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
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
from fastapi import (
    Path as ApiPath,
)
from fastapi.responses import FileResponse, ORJSONResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from dca.claude import (
    ClaudeCode,
    ClaudeError,
    ClaudeOAuthManager,
    validate_claude_oauth_token,
)
from dca.config import Settings, get_settings
from dca.db import (
    AdminAccessKey,
    AdminPrincipal,
    AdminSession,
    AuditEvent,
    ChangeRequest,
    Clarification,
    ConversationMemory,
    ConversationMessage,
    ConversationThread,
    Database,
    Interaction,
    Job,
    Project,
    ProjectAgentSettings,
    ProjectMembership,
    Repository,
    ServiceAccount,
    ServiceAccountProject,
    SystemSecret,
    TelegramIdentity,
    User,
    append_audit,
    enqueue_repository_sync,
)
from dca.domain import (
    ChangeRequestStatus,
    ClarificationStatus,
    JobStatus,
    RepositoryStatus,
    utcnow,
)
from dca.mcp import MCP_TOOL_SCOPES, build_mcp, generate_service_token
from dca.privacy import sanitize_text
from dca.repositories import normalize_github_repository, verify_github_webhook_signature
from dca.service import (
    SYSTEM_SECRET_CLAUDE_OAUTH,
    ServiceError,
    admin_key_fingerprint,
    encrypt_system_secret,
    load_system_secret,
    update_change_request_status,
    validate_admin_access_key,
)
from dca.telegram import TelegramAdapter, ingest_telegram_update
from dca.worker import configure_logging

log = structlog.get_logger()
SESSION_COOKIE = "dca_admin"
SESSION_MAX_AGE = 180 * 24 * 60 * 60


def _purge_claude_session_artifacts(root: Path, session_id: UUID) -> int:
    """Delete only Claude artifacts whose basename is the exact session UUID."""
    resolved_root = root.expanduser().resolve()
    if not resolved_root.exists():
        return 0
    if not resolved_root.is_dir() or resolved_root == Path(resolved_root.anchor):
        raise RuntimeError("unsafe Claude session root")

    session_name = str(session_id)
    allowed_names = {session_name, f"{session_name}.jsonl"}
    candidates: list[Path] = []
    for directory, directory_names, file_names in os.walk(
        resolved_root, topdown=True, followlinks=False
    ):
        parent = Path(directory)
        candidates.extend(parent / name for name in directory_names if name in allowed_names)
        candidates.extend(parent / name for name in file_names if name in allowed_names)

    unique_candidates = sorted(set(candidates), key=lambda path: len(path.parts), reverse=True)
    for candidate in unique_candidates:
        if candidate.is_symlink():
            raise RuntimeError("unsafe Claude session artifact symlink")
        try:
            candidate.resolve(strict=True).relative_to(resolved_root)
        except (FileNotFoundError, ValueError) as exc:
            raise RuntimeError("unsafe Claude session artifact path") from exc
        if candidate.name == session_name and not candidate.is_dir():
            raise RuntimeError("invalid Claude session artifact directory")
        if candidate.name.endswith(".jsonl") and not candidate.is_file():
            raise RuntimeError("invalid Claude session transcript")

    deleted = 0
    for candidate in unique_candidates:
        if not candidate.exists():
            continue
        if candidate.is_symlink():
            raise RuntimeError("unsafe Claude session artifact symlink")
        if candidate.is_dir():
            shutil.rmtree(candidate)
        else:
            candidate.unlink()
        deleted += 1
    return deleted


async def read_capped_request_body(
    request: Request,
    *,
    limit: int,
    too_large_detail: str,
) -> bytes:
    content_length = request.headers.get("content-length")
    try:
        declared_length = int(content_length) if content_length else 0
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_content_length") from exc
    if declared_length < 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_content_length")
    if declared_length > limit:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, too_large_detail)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, too_large_detail)
        body.extend(chunk)
    return bytes(body)


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


class MemberUpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=160)
    telegram_user_id: int = Field(gt=0)
    telegram_username: str | None = Field(default=None, max_length=64)
    role: str = Field(min_length=1, max_length=40)
    department: str | None = Field(default=None, max_length=80)
    stack: str | None = Field(default=None, max_length=160)
    language: Literal["ru", "en"]
    knowledge_scope: Literal["integration", "internal"]
    can_create_requests: bool
    active: bool

    @field_validator("display_name", "role")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value cannot be blank")
        return normalized

    @field_validator("department", "stack")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("telegram_username")
    @classmethod
    def normalize_telegram_username(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().removeprefix("@")
        if not normalized:
            return None
        if re.fullmatch(r"[A-Za-z0-9_]{1,64}", normalized) is None:
            raise ValueError("telegram username must contain only letters, digits and underscore")
        return normalized


class AgentSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=0)
    enabled: bool
    claude_model: str | None = Field(default=None, max_length=120)
    claude_effort: Literal["low", "medium", "high", "xhigh", "max"]
    claude_timeout_seconds: int = Field(ge=10, le=900)
    max_budget_cents: int | None = Field(default=None, gt=0)
    base_prompt: str = Field(max_length=20_000)
    answer_style: Literal["brief", "normal", "detailed"]
    privacy_level: Literal["strict", "balanced"]
    denied_globs: list[str] = Field(max_length=200)
    memory_enabled: bool
    memory_recent_messages: int = Field(ge=4, le=100)
    memory_max_context_chars: int = Field(ge=3_000, le=100_000)
    telegram_group_mode: Literal["commands_only", "mentions", "all_messages"]
    telegram_private_mode: Literal["commands_only", "all_messages"]
    telegram_streaming_enabled: bool
    telegram_attach_markdown: bool

    @field_validator("claude_model")
    @classmethod
    def normalize_claude_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("base_prompt")
    @classmethod
    def reject_credentials_in_base_prompt(cls, value: str) -> str:
        result = sanitize_text(value, level="strict", location="agent_settings.base_prompt")
        if result.findings:
            raise ValueError("base prompt must not contain credential material")
        return value

    @field_validator("denied_globs")
    @classmethod
    def validate_denied_globs(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(
            not value
            or len(value) > 500
            or not value.isascii()
            or not value.isprintable()
            or value.startswith(("/", ":"))
            or "\\" in value
            or any(part == ".." for part in value.split("/"))
            or re.fullmatch(r"[A-Za-z0-9._*/?+ -]+", value) is None
            for value in normalized
        ):
            raise ValueError("denied globs must be safe relative Git glob patterns")
        if len(set(normalized)) != len(normalized):
            raise ValueError("denied globs must be unique")
        return normalized


class ClaudeIntegrationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    oauth_token: SecretStr = Field(max_length=16_384)


class ClaudeIntegrationStatus(BaseModel):
    configured: bool
    source: Literal["panel", "environment", "missing"]
    proxy_configured: bool


class ClaudeOAuthStartInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClaudeOAuthStartResponse(BaseModel):
    session_id: str
    authorization_url: str
    expires_at: datetime


class ClaudeOAuthCompleteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    code: SecretStr = Field(min_length=1, max_length=4_096)

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: SecretStr) -> SecretStr:
        normalized = value.get_secret_value().strip()
        if (
            not normalized
            or not normalized.isascii()
            or not normalized.isprintable()
            or any(character in normalized for character in "\r\n\0")
        ):
            raise ValueError("Claude OAuth code must be printable single-line ASCII")
        return SecretStr(normalized)


class McpAccountCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    tool_scopes: list[str] = Field(min_length=1, max_length=len(MCP_TOOL_SCOPES))
    project_ids: list[UUID] = Field(min_length=1, max_length=100)
    expires_at: datetime | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name cannot be blank")
        return normalized

    @field_validator("tool_scopes")
    @classmethod
    def validate_scopes(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or not set(values) <= MCP_TOOL_SCOPES:
            raise ValueError("tool scopes must be unique and supported")
        return sorted(values)

    @field_validator("project_ids")
    @classmethod
    def validate_projects(cls, values: list[UUID]) -> list[UUID]:
        if len(set(values)) != len(values):
            raise ValueError("project IDs must be unique")
        return values


class McpAccountPatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    active: bool | None = None
    tool_scopes: list[str] | None = Field(
        default=None, min_length=1, max_length=len(MCP_TOOL_SCOPES)
    )
    project_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=100)
    expires_at: datetime | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("name cannot be blank")
        return normalized

    @field_validator("tool_scopes")
    @classmethod
    def validate_scopes(cls, values: list[str] | None) -> list[str] | None:
        if values is not None and (
            len(set(values)) != len(values) or not set(values) <= MCP_TOOL_SCOPES
        ):
            raise ValueError("tool scopes must be unique and supported")
        return sorted(values) if values is not None else None

    @field_validator("project_ids")
    @classmethod
    def validate_projects(cls, values: list[UUID] | None) -> list[UUID] | None:
        if values is not None and len(set(values)) != len(values):
            raise ValueError("project IDs must be unique")
        return values


class McpAccountRotateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    database = Database(settings)
    telegram = TelegramAdapter(settings, database)
    claude_oauth = ClaudeOAuthManager(settings)
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
        await claude_oauth.close()
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
    app.state.claude_oauth = claude_oauth
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

    async def append_claude_oauth_audit(
        *,
        event_type: str,
        admin: AdminContext,
        session_id: str | None,
        outcome: str = "success",
        error_code: str | None = None,
        expires_at: datetime | None = None,
        cancelled: bool | None = None,
    ) -> None:
        payload: dict[str, Any] = {}
        if error_code is not None:
            payload["error_code"] = error_code
        if expires_at is not None:
            payload["expires_at"] = expires_at.isoformat()
        if cancelled is not None:
            payload["cancelled"] = cancelled
        async with database.session() as session:
            await append_audit(
                session,
                event_type=event_type,
                correlation_id=f"claude-oauth:{secrets.token_hex(8)}",
                actor_type="admin",
                actor_id=str(admin.principal_id),
                subject_type="claude_oauth_session",
                subject_id=(
                    oauth_session_fingerprint(session_id) if session_id is not None else None
                ),
                outcome=outcome,
                payload=payload,
            )

    async def store_claude_oauth_token(
        session: Any,
        provider_value: str,
        admin_id: UUID,
    ) -> None:
        ciphertext = encrypt_system_secret(provider_value, raw_session_secret)
        managed = await session.get(SystemSecret, SYSTEM_SECRET_CLAUDE_OAUTH)
        if managed is None:
            session.add(
                SystemSecret(
                    name=SYSTEM_SECRET_CLAUDE_OAUTH,
                    ciphertext=ciphertext,
                    updated_by=admin_id,
                )
            )
            return
        managed.ciphertext = ciphertext
        managed.updated_by = admin_id
        managed.updated_at = utcnow()

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
        body = await read_capped_request_body(
            request,
            limit=settings.max_telegram_body_bytes,
            too_large_detail="update_too_large",
        )
        try:
            payload = orjson.loads(body)
            payload["update_id"] = int(payload["update_id"])
        except (KeyError, TypeError, ValueError, orjson.JSONDecodeError) as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_update") from exc

        async with database.session() as session:
            await ingest_telegram_update(session, telegram, payload, actor_id="webhook")
        return {"ok": True}

    @app.post(
        "/webhooks/github",
        status_code=status.HTTP_202_ACCEPTED,
        include_in_schema=False,
    )
    async def github_webhook(
        request: Request,
        x_hub_signature_256: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
        x_github_event: Annotated[str | None, Header(alias="X-GitHub-Event")] = None,
        x_github_delivery: Annotated[str | None, Header(alias="X-GitHub-Delivery")] = None,
    ) -> dict[str, Any]:
        secret = settings.github_webhook_secret.get_secret_value()
        if not secret:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "github_webhook_not_configured"
            )
        body = await read_capped_request_body(
            request,
            limit=settings.max_github_webhook_body_bytes,
            too_large_detail="payload_too_large",
        )
        if not verify_github_webhook_signature(secret, body, x_hub_signature_256):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_webhook_signature")
        if not x_github_delivery or re.fullmatch(r"[A-Za-z0-9-]{1,128}", x_github_delivery) is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_delivery_id")
        try:
            payload = orjson.loads(body)
            full_name = payload["repository"]["full_name"]
            if not isinstance(full_name, str):
                raise ValueError("repository full_name must be a string")
            repository_name = normalize_github_repository(full_name)
        except (KeyError, TypeError, ValueError, orjson.JSONDecodeError) as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_github_payload"
            ) from exc

        async with database.session() as session:
            repositories = list(
                await session.scalars(
                    select(Repository)
                    .where(Repository.github_repository == repository_name)
                    .order_by(Repository.id)
                    .with_for_update()
                )
            )
            if not repositories:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "repository_not_found")
            received_at = utcnow()
            for repository in repositories:
                repository.last_webhook_at = received_at
            if x_github_event == "ping":
                return {"ok": True, "queued": False, "reason": "ping"}
            if x_github_event != "push":
                return {"ok": True, "queued": False, "reason": "event_ignored"}
            commit = payload.get("after")
            if not isinstance(commit, str) or re.fullmatch(r"[0-9a-fA-F]{40}", commit) is None:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_push_commit")
            commit = commit.casefold()
            if payload.get("deleted") is True or commit == "0" * 40:
                return {"ok": True, "queued": False, "reason": "branch_deleted"}
            payload_digest = hashlib.sha256(body).hexdigest()
            jobs: list[dict[str, Any]] = []
            enabled_count = 0
            for repository in repositories:
                if (
                    not repository.auto_sync_enabled
                    or repository.status == RepositoryStatus.DISABLED.value
                ):
                    continue
                enabled_count += 1
                expected_ref = f"refs/heads/{repository.default_branch}"
                if payload.get("ref") != expected_ref:
                    continue
                repository.last_webhook_commit = commit
                deduplication_key = f"github:{repository.id}:{payload_digest}"
                job, queued = await enqueue_repository_sync(
                    session,
                    repository=repository,
                    source="github",
                    requested_commit=commit,
                    deduplication_key=deduplication_key,
                )
                if queued:
                    if repository.status != RepositoryStatus.SYNCING.value:
                        repository.status = RepositoryStatus.STALE.value
                    await append_audit(
                        session,
                        event_type="repository.sync_requested",
                        correlation_id=f"github:{x_github_delivery}",
                        actor_type="github",
                        actor_id=repository_name,
                        project_id=repository.project_id,
                        subject_type="repository",
                        subject_id=str(repository.id),
                        payload={"commit": commit, "ref": expected_ref, "source": "webhook"},
                    )
                jobs.append(
                    {
                        "repository_id": str(repository.id),
                        "job_id": str(job.id),
                        "status": job.status,
                        "queued": queued,
                    }
                )
            if not jobs:
                reason = "auto_sync_disabled" if enabled_count == 0 else "branch_ignored"
                return {"ok": True, "queued": False, "reason": reason}
        first_job = jobs[0]
        return {
            "ok": True,
            "queued": any(item["queued"] for item in jobs),
            "job_id": first_job["job_id"],
            "status": first_job["status"],
            "jobs": jobs,
        }

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

    @app.get("/api/v1/members")
    async def list_members(
        _: Annotated[AdminContext, Depends(require_admin)],
        project_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            select(User, ProjectMembership, TelegramIdentity)
            .join(ProjectMembership, ProjectMembership.user_id == User.id)
            .outerjoin(TelegramIdentity, TelegramIdentity.user_id == User.id)
            .order_by(
                ProjectMembership.project_id,
                User.display_name,
                TelegramIdentity.created_at.desc().nullslast(),
            )
        )
        if project_id is not None:
            query = query.where(ProjectMembership.project_id == project_id)
        async with database.session() as session:
            rows = (await session.execute(query)).all()
        # A user normally has one Telegram identity. If old data contains more,
        # expose the newest identity once instead of duplicating the membership row.
        members: dict[tuple[UUID, UUID], dict[str, Any]] = {}
        for user, membership, identity in rows:
            key = (membership.project_id, user.id)
            members.setdefault(key, serialize_member(user, membership, identity))
        return list(members.values())

    @app.put("/api/v1/projects/{project_id}/members/{user_id}")
    async def update_member(
        project_id: UUID,
        user_id: UUID,
        payload: MemberUpdateInput,
        admin: Annotated[AdminContext, Depends(require_admin)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> dict[str, Any]:
        try:
            async with database.session() as session:
                row = (
                    await session.execute(
                        select(User, ProjectMembership)
                        .join(ProjectMembership, ProjectMembership.user_id == User.id)
                        .where(
                            User.id == user_id,
                            ProjectMembership.project_id == project_id,
                        )
                        .with_for_update()
                    )
                ).one_or_none()
                if row is None:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, "member_not_found")
                user, membership = row
                identity = await session.scalar(
                    select(TelegramIdentity)
                    .where(TelegramIdentity.user_id == user_id)
                    .order_by(TelegramIdentity.created_at.desc())
                    .limit(1)
                    .with_for_update()
                )
                previous_telegram_user_id = (
                    identity.telegram_user_id if identity is not None else None
                )
                previous_telegram_username = identity.username if identity is not None else None
                telegram_user_id_changed = (
                    identity is not None and identity.telegram_user_id != payload.telegram_user_id
                )
                if identity is None:
                    identity = TelegramIdentity(
                        user_id=user_id,
                        telegram_user_id=payload.telegram_user_id,
                        username=payload.telegram_username,
                        reachable=False,
                    )
                    session.add(identity)
                else:
                    identity.telegram_user_id = payload.telegram_user_id
                    identity.username = payload.telegram_username
                    if telegram_user_id_changed:
                        identity.verified_at = None
                        identity.reachable = False
                        identity.private_chat_id = None

                user.display_name = payload.display_name
                user.active = payload.active
                membership.role = payload.role
                membership.department = payload.department
                membership.stack = payload.stack
                membership.preferred_language = payload.language
                membership.knowledge_scope = payload.knowledge_scope
                membership.can_create_requests = payload.can_create_requests
                await session.flush()
                await append_audit(
                    session,
                    event_type="project.member_profile_updated",
                    correlation_id=(
                        f"member-profile:{project_id}:{user_id}:{secrets.token_hex(8)}"
                    ),
                    actor_type="admin",
                    actor_id=str(admin.principal_id),
                    project_id=project_id,
                    subject_type="user",
                    subject_id=str(user_id),
                    payload={
                        "role": membership.role,
                        "department": membership.department,
                        "stack": membership.stack,
                        "language": membership.preferred_language,
                        "knowledge_scope": membership.knowledge_scope,
                        "can_create_requests": membership.can_create_requests,
                        "active": user.active,
                        "telegram_user_id_previous": previous_telegram_user_id,
                        "telegram_user_id": identity.telegram_user_id,
                        "telegram_user_id_changed": telegram_user_id_changed,
                        "telegram_username_changed": (
                            previous_telegram_username != identity.username
                        ),
                        "telegram_verification_reset": telegram_user_id_changed,
                    },
                )
                return serialize_member(user, membership, identity)
        except IntegrityError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "telegram_user_id_already_assigned"
            ) from exc

    @app.get("/api/v1/projects/{project_id}/agent-settings")
    async def get_agent_settings(
        project_id: UUID,
        _: Annotated[AdminContext, Depends(require_admin)],
    ) -> dict[str, Any]:
        async with database.session() as session:
            if await session.get(Project, project_id) is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "project_not_found")
            row = await session.get(ProjectAgentSettings, project_id)
        return serialize_agent_settings(row, project_id)

    @app.put("/api/v1/projects/{project_id}/agent-settings")
    async def put_agent_settings(
        project_id: UUID,
        payload: AgentSettingsInput,
        admin: Annotated[AdminContext, Depends(require_admin)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> dict[str, Any]:
        async with database.session() as session:
            project = await session.scalar(
                select(Project).where(Project.id == project_id).with_for_update()
            )
            if project is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "project_not_found")
            row = await session.get(ProjectAgentSettings, project_id)
            if row is None:
                if payload.expected_version != 0:
                    raise HTTPException(status.HTTP_409_CONFLICT, "version_conflict")
                row = ProjectAgentSettings(project_id=project_id, version=1)
                session.add(row)
            else:
                if row.version != payload.expected_version:
                    raise HTTPException(status.HTTP_409_CONFLICT, "version_conflict")
                row.version += 1
            row.enabled = payload.enabled
            row.claude_model = payload.claude_model
            row.claude_effort = payload.claude_effort
            row.claude_timeout_seconds = payload.claude_timeout_seconds
            row.max_budget_cents = payload.max_budget_cents
            row.base_prompt = payload.base_prompt
            row.answer_style = payload.answer_style
            row.privacy_level = payload.privacy_level
            row.denied_globs = payload.denied_globs
            row.memory_enabled = payload.memory_enabled
            row.memory_recent_messages = payload.memory_recent_messages
            row.memory_max_context_chars = payload.memory_max_context_chars
            row.telegram_group_mode = payload.telegram_group_mode
            row.telegram_private_mode = payload.telegram_private_mode
            row.telegram_streaming_enabled = payload.telegram_streaming_enabled
            row.telegram_attach_markdown = payload.telegram_attach_markdown
            row.updated_by_admin_id = admin.principal_id
            row.updated_at = utcnow()
            await session.flush()
            await append_audit(
                session,
                event_type="project.agent_settings_updated",
                correlation_id=f"agent-settings:{project_id}:{row.version}:{secrets.token_hex(8)}",
                actor_type="admin",
                actor_id=str(admin.principal_id),
                project_id=project_id,
                subject_type="project_agent_settings",
                subject_id=str(project_id),
                payload={
                    "version": row.version,
                    "enabled": row.enabled,
                    "privacy_level": row.privacy_level,
                    "memory_enabled": row.memory_enabled,
                },
            )
            result = serialize_agent_settings(row, project_id)
        return result

    @app.post(
        "/api/v1/integrations/claude/oauth/start",
        response_model=ClaudeOAuthStartResponse,
    )
    async def start_claude_oauth(
        _: ClaudeOAuthStartInput,
        admin: Annotated[AdminContext, Depends(require_admin)],
        __: Annotated[None, Depends(require_same_origin)],
    ) -> dict[str, Any]:
        try:
            started = await claude_oauth.start(admin.principal_id)
        except ClaudeError as exc:
            await append_claude_oauth_audit(
                event_type="claude.oauth_start_failed",
                admin=admin,
                session_id=None,
                outcome="failure",
                error_code=exc.code,
            )
            raise claude_oauth_http_exception(exc) from exc
        try:
            await append_claude_oauth_audit(
                event_type="claude.oauth_started",
                admin=admin,
                session_id=started.session_id,
                expires_at=started.expires_at,
            )
        except BaseException:
            await claude_oauth.cancel(admin.principal_id, started.session_id)
            raise
        return {
            "session_id": started.session_id,
            "authorization_url": started.authorization_url,
            "expires_at": started.expires_at,
        }

    @app.post(
        "/api/v1/integrations/claude/oauth/complete",
        response_model=ClaudeIntegrationStatus,
    )
    async def complete_claude_oauth(
        payload: ClaudeOAuthCompleteInput,
        admin: Annotated[AdminContext, Depends(require_admin)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> dict[str, Any]:
        try:
            provider_value = await claude_oauth.complete(
                admin.principal_id,
                payload.session_id,
                payload.code.get_secret_value(),
            )
        except ClaudeError as exc:
            await append_claude_oauth_audit(
                event_type="claude.oauth_complete_failed",
                admin=admin,
                session_id=payload.session_id,
                outcome="failure",
                error_code=exc.code,
            )
            raise claude_oauth_http_exception(exc) from exc
        try:
            async with database.session() as session:
                await store_claude_oauth_token(session, provider_value, admin.principal_id)
                await append_audit(
                    session,
                    event_type="claude.oauth_completed",
                    correlation_id=f"claude-oauth:{secrets.token_hex(8)}",
                    actor_type="admin",
                    actor_id=str(admin.principal_id),
                    subject_type="claude_oauth_session",
                    subject_id=oauth_session_fingerprint(payload.session_id),
                    payload={"source": "panel"},
                )
        finally:
            provider_value = ""
        return claude_integration_status(settings, panel_configured=True)

    @app.delete(
        "/api/v1/integrations/claude/oauth/{session_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def cancel_claude_oauth(
        session_id: Annotated[
            str,
            ApiPath(min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
        ],
        admin: Annotated[AdminContext, Depends(require_admin)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> Response:
        cancelled = await claude_oauth.cancel(admin.principal_id, session_id)
        await append_claude_oauth_audit(
            event_type="claude.oauth_cancelled",
            admin=admin,
            session_id=session_id,
            cancelled=cancelled,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/api/v1/integrations/claude")
    async def get_claude_integration(
        _: Annotated[AdminContext, Depends(require_admin)],
    ) -> dict[str, Any]:
        async with database.session() as session:
            managed = await session.get(SystemSecret, SYSTEM_SECRET_CLAUDE_OAUTH)
        return claude_integration_status(settings, panel_configured=managed is not None)

    @app.put("/api/v1/integrations/claude")
    async def put_claude_integration(
        payload: ClaudeIntegrationInput,
        admin: Annotated[AdminContext, Depends(require_admin)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> dict[str, Any]:
        try:
            token = validate_claude_oauth_token(payload.oauth_token.get_secret_value())
        except ClaudeError as exc:
            raise claude_oauth_http_exception(exc) from exc
        async with database.session() as session:
            await store_claude_oauth_token(session, token, admin.principal_id)
            await append_audit(
                session,
                event_type="claude.integration_updated",
                correlation_id=f"claude-integration:{secrets.token_hex(8)}",
                actor_type="admin",
                actor_id=str(admin.principal_id),
                subject_type="system_secret",
                subject_id=SYSTEM_SECRET_CLAUDE_OAUTH,
            )
        return claude_integration_status(settings, panel_configured=True)

    @app.delete("/api/v1/integrations/claude")
    async def delete_claude_integration(
        admin: Annotated[AdminContext, Depends(require_admin)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> dict[str, Any]:
        async with database.session() as session:
            managed = await session.get(SystemSecret, SYSTEM_SECRET_CLAUDE_OAUTH)
            if managed is not None:
                await session.delete(managed)
            await append_audit(
                session,
                event_type="claude.integration_deleted",
                correlation_id=f"claude-integration:{secrets.token_hex(8)}",
                actor_type="admin",
                actor_id=str(admin.principal_id),
                subject_type="system_secret",
                subject_id=SYSTEM_SECRET_CLAUDE_OAUTH,
                payload={"removed": managed is not None},
            )
        return claude_integration_status(settings, panel_configured=False)

    @app.post("/api/v1/integrations/claude/check")
    async def check_claude_integration(
        admin: Annotated[AdminContext, Depends(require_admin)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> dict[str, Any]:
        source = "unknown"
        version: str | None = None
        error_code: str | None = None
        try:
            async with database.session() as session:
                token = await load_system_secret(
                    session,
                    SYSTEM_SECRET_CLAUDE_OAUTH,
                    raw_session_secret,
                )
            if token is not None:
                source = "panel"
            elif os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
                source = "environment"
            else:
                source = "missing"
            version = await ClaudeCode(settings).probe(oauth_token=token)
        except Exception as exc:
            log.warning("claude.probe_failed", error_type=type(exc).__name__)
            error_code = exc.code if isinstance(exc, ClaudeError) else "model_provider_unavailable"
            outcome = "failure"
        else:
            outcome = "success"
        async with database.session() as session:
            await append_audit(
                session,
                event_type="claude.integration_checked",
                correlation_id=f"claude-integration-check:{secrets.token_hex(8)}",
                actor_type="admin",
                actor_id=str(admin.principal_id),
                subject_type="system_secret",
                subject_id=SYSTEM_SECRET_CLAUDE_OAUTH,
                outcome=outcome,
                payload={"source": source, "version": version, "error_code": error_code},
            )
        return {"ok": outcome == "success", "version": version, "error_code": error_code}

    @app.get("/api/v1/mcp/accounts")
    async def list_mcp_accounts(
        _: Annotated[AdminContext, Depends(require_admin)],
        project_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        query = select(ServiceAccount).order_by(ServiceAccount.name)
        if project_id is not None:
            query = query.join(ServiceAccountProject).where(
                ServiceAccountProject.project_id == project_id
            )
        async with database.session() as session:
            accounts = list(await session.scalars(query))
            return [await serialize_mcp_account(session, account) for account in accounts]

    @app.post("/api/v1/mcp/accounts", status_code=status.HTTP_201_CREATED)
    async def create_mcp_account(
        payload: McpAccountCreateInput,
        admin: Annotated[AdminContext, Depends(require_admin)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> dict[str, Any]:
        validate_future_expiry(payload.expires_at)
        token, prefix, token_hash = generate_service_token()
        try:
            async with database.session() as session:
                await require_projects(session, payload.project_ids)
                account = ServiceAccount(
                    name=payload.name,
                    token_prefix=prefix,
                    token_hash=token_hash,
                    tool_scopes=payload.tool_scopes,
                    expires_at=payload.expires_at,
                    version=1,
                )
                session.add(account)
                await session.flush()
                session.add_all(
                    ServiceAccountProject(service_account_id=account.id, project_id=project_id)
                    for project_id in payload.project_ids
                )
                await session.flush()
                await append_audit(
                    session,
                    event_type="mcp.service_account_created",
                    correlation_id=f"mcp-account:{account.id}:{secrets.token_hex(8)}",
                    actor_type="admin",
                    actor_id=str(admin.principal_id),
                    subject_type="service_account",
                    subject_id=str(account.id),
                    payload={
                        "name": account.name,
                        "project_ids": sorted(str(value) for value in payload.project_ids),
                        "tool_scopes": account.tool_scopes,
                    },
                )
                serialized = await serialize_mcp_account(session, account)
        except IntegrityError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, "service_account_conflict") from exc
        return {"account": serialized, "token": token}

    @app.patch("/api/v1/mcp/accounts/{account_id}")
    async def patch_mcp_account(
        account_id: UUID,
        payload: McpAccountPatchInput,
        admin: Annotated[AdminContext, Depends(require_admin)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> dict[str, Any]:
        changed = payload.model_fields_set - {"expected_version"}
        if not changed:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "no_changes")
        if payload.expires_at is not None:
            validate_future_expiry(payload.expires_at)
        for required in ("name", "active", "tool_scopes", "project_ids"):
            if required in changed and getattr(payload, required) is None:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    f"{required}_cannot_be_null",
                )
        try:
            async with database.session() as session:
                account = await session.scalar(
                    select(ServiceAccount).where(ServiceAccount.id == account_id).with_for_update()
                )
                if account is None:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, "service_account_not_found")
                if account.version != payload.expected_version:
                    raise HTTPException(status.HTTP_409_CONFLICT, "version_conflict")
                if "name" in changed:
                    account.name = payload.name or ""
                if "active" in changed:
                    account.active = bool(payload.active)
                if "tool_scopes" in changed:
                    account.tool_scopes = payload.tool_scopes or []
                if "expires_at" in changed:
                    account.expires_at = payload.expires_at
                if "project_ids" in changed:
                    project_ids = payload.project_ids or []
                    await require_projects(session, project_ids)
                    current = {
                        association.project_id: association
                        for association in await session.scalars(
                            select(ServiceAccountProject).where(
                                ServiceAccountProject.service_account_id == account.id
                            )
                        )
                    }
                    requested = set(project_ids)
                    for project_id in current.keys() - requested:
                        await session.delete(current[project_id])
                    session.add_all(
                        ServiceAccountProject(
                            service_account_id=account.id,
                            project_id=project_id,
                        )
                        for project_id in requested - current.keys()
                    )
                account.version += 1
                account.updated_at = utcnow()
                await session.flush()
                await append_audit(
                    session,
                    event_type="mcp.service_account_updated",
                    correlation_id=f"mcp-account:{account.id}:{secrets.token_hex(8)}",
                    actor_type="admin",
                    actor_id=str(admin.principal_id),
                    subject_type="service_account",
                    subject_id=str(account.id),
                    payload={"changed": sorted(changed), "version": account.version},
                )
                serialized = await serialize_mcp_account(session, account)
        except IntegrityError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, "service_account_conflict") from exc
        return serialized

    @app.post("/api/v1/mcp/accounts/{account_id}/rotate-token")
    async def rotate_mcp_account_token(
        account_id: UUID,
        payload: McpAccountRotateInput,
        admin: Annotated[AdminContext, Depends(require_admin)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> dict[str, Any]:
        token, prefix, token_hash = generate_service_token()
        try:
            async with database.session() as session:
                account = await session.scalar(
                    select(ServiceAccount).where(ServiceAccount.id == account_id).with_for_update()
                )
                if account is None:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, "service_account_not_found")
                if account.version != payload.expected_version:
                    raise HTTPException(status.HTTP_409_CONFLICT, "version_conflict")
                account.token_prefix = prefix
                account.token_hash = token_hash
                account.version += 1
                account.updated_at = utcnow()
                await session.flush()
                await append_audit(
                    session,
                    event_type="mcp.service_account_token_rotated",
                    correlation_id=f"mcp-account:{account.id}:{secrets.token_hex(8)}",
                    actor_type="admin",
                    actor_id=str(admin.principal_id),
                    subject_type="service_account",
                    subject_id=str(account.id),
                    payload={"version": account.version},
                )
                serialized = await serialize_mcp_account(session, account)
        except IntegrityError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, "service_account_conflict") from exc
        return {"account": serialized, "token": token}

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

    @app.get("/api/v1/conversations")
    async def list_conversations(
        _: Annotated[AdminContext, Depends(require_admin)],
        project_id: UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, Any]]:
        message_count = (
            select(func.count())
            .select_from(ConversationMessage)
            .where(ConversationMessage.thread_id == ConversationThread.id)
            .correlate(ConversationThread)
            .scalar_subquery()
        )
        memory_count = (
            select(func.count())
            .select_from(ConversationMemory)
            .where(ConversationMemory.thread_id == ConversationThread.id)
            .correlate(ConversationThread)
            .scalar_subquery()
        )
        query = (
            select(
                ConversationThread,
                User.display_name,
                message_count.label("message_count"),
                memory_count.label("memory_count"),
            )
            .outerjoin(User, User.id == ConversationThread.user_id)
            .order_by(
                ConversationThread.last_message_at.desc().nullslast(),
                ConversationThread.created_at.desc(),
            )
        )
        if project_id is not None:
            query = query.where(ConversationThread.project_id == project_id)
        async with database.session() as session:
            rows = (await session.execute(query.limit(limit).offset(offset))).all()
        return [
            serialize_conversation_summary(
                thread,
                user_display_name=user_display_name,
                message_count=int(messages),
                memory_count=int(memories),
            )
            for thread, user_display_name, messages, memories in rows
        ]

    @app.get("/api/v1/conversations/{thread_id}")
    async def conversation_detail(
        thread_id: UUID,
        _: Annotated[AdminContext, Depends(require_admin)],
    ) -> dict[str, Any]:
        async with database.session() as session:
            thread = await session.get(ConversationThread, thread_id)
            if thread is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation_not_found")
            user_display_name = (
                await session.scalar(select(User.display_name).where(User.id == thread.user_id))
                if thread.user_id is not None
                else None
            )
            messages = list(
                await session.scalars(
                    select(ConversationMessage)
                    .where(ConversationMessage.thread_id == thread.id)
                    .order_by(
                        ConversationMessage.created_at.desc(),
                        ConversationMessage.id.desc(),
                    )
                    .limit(500)
                )
            )
            messages.reverse()
            memories = list(
                await session.scalars(
                    select(ConversationMemory)
                    .where(ConversationMemory.thread_id == thread.id)
                    .order_by(
                        ConversationMemory.kind,
                        ConversationMemory.updated_at.desc(),
                        ConversationMemory.id,
                    )
                    .limit(200)
                )
            )
            message_count = await session.scalar(
                select(func.count())
                .select_from(ConversationMessage)
                .where(ConversationMessage.thread_id == thread.id)
            )
            memory_count = await session.scalar(
                select(func.count())
                .select_from(ConversationMemory)
                .where(ConversationMemory.thread_id == thread.id)
            )
        return {
            **serialize_conversation_summary(
                thread,
                user_display_name=user_display_name,
                message_count=int(message_count or 0),
                memory_count=int(memory_count or 0),
            ),
            "messages": [serialize_conversation_message(row) for row in messages],
            "memories": [serialize_conversation_memory(row) for row in memories],
        }

    @app.delete(
        "/api/v1/conversations/{thread_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_conversation(
        thread_id: UUID,
        admin: Annotated[AdminContext, Depends(require_admin)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> Response:
        lock_key = f"dca:claude-context:{thread_id}"
        async with database.engine.connect() as lock_connection:
            acquired = await lock_connection.scalar(
                text("SELECT pg_try_advisory_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": lock_key},
            )
            if acquired is not True:
                raise HTTPException(status.HTTP_409_CONFLICT, "conversation_context_busy")
            try:
                async with database.session() as session:
                    thread = await session.scalar(
                        select(ConversationThread)
                        .where(ConversationThread.id == thread_id)
                        .with_for_update()
                    )
                    if thread is None:
                        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation_not_found")
                    project_id = thread.project_id
                    claude_session_id = thread.claude_session_id
                    try:
                        deleted_artifacts = (
                            await asyncio.to_thread(
                                _purge_claude_session_artifacts,
                                settings.claude_session_root,
                                claude_session_id,
                            )
                            if claude_session_id is not None
                            else 0
                        )
                    except RuntimeError as exc:
                        log.exception(
                            "conversation.claude_session_cleanup_failed",
                            thread_id=str(thread.id),
                        )
                        raise HTTPException(
                            status.HTTP_500_INTERNAL_SERVER_ERROR,
                            "claude_session_cleanup_failed",
                        ) from exc
                    await append_audit(
                        session,
                        event_type="conversation.deleted",
                        correlation_id=f"conversation:{thread.id}:{secrets.token_hex(8)}",
                        actor_type="admin",
                        actor_id=str(admin.principal_id),
                        project_id=project_id,
                        subject_type="conversation_thread",
                        subject_id=str(thread.id),
                        payload={
                            "chat_id": str(thread.chat_id) if thread.chat_id else None,
                            "claude_session_id": (
                                str(claude_session_id) if claude_session_id is not None else None
                            ),
                            "claude_session_artifacts_deleted": deleted_artifacts,
                        },
                    )
                    await session.delete(thread)
            finally:
                try:
                    released = await lock_connection.scalar(
                        text("SELECT pg_advisory_unlock(hashtextextended(:lock_key, 0))"),
                        {"lock_key": lock_key},
                    )
                except Exception as exc:
                    log.exception(
                        "claude.context_lock_release_failed",
                        thread_id=str(thread_id),
                    )
                    await lock_connection.invalidate(exc)
                else:
                    if released is not True:
                        log.error(
                            "claude.context_lock_release_failed",
                            thread_id=str(thread_id),
                        )
                        await lock_connection.invalidate()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/api/v1/interactions")
    async def list_interactions(
        _: Annotated[AdminContext, Depends(require_admin)],
        project_id: UUID | None = None,
        interaction_status: Annotated[str | None, Query(alias="status", max_length=32)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, Any]]:
        query = select(Interaction).order_by(Interaction.created_at.desc())
        if project_id is not None:
            query = query.where(Interaction.project_id == project_id)
        if interaction_status is not None:
            query = query.where(Interaction.status == interaction_status)
        async with database.session() as session:
            rows = list(await session.scalars(query.limit(limit).offset(offset)))
        return [serialize_interaction_summary(row) for row in rows]

    @app.get("/api/v1/interactions/{interaction_id}")
    async def interaction_detail(
        interaction_id: UUID,
        _: Annotated[AdminContext, Depends(require_admin)],
    ) -> dict[str, Any]:
        async with database.session() as session:
            row = await session.get(Interaction, interaction_id)
            if row is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "interaction_not_found")
        return serialize_interaction(row)

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
        return [serialize_repository(row, settings) for row in rows]

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
            if repository.status == RepositoryStatus.DISABLED.value:
                raise HTTPException(status.HTTP_409_CONFLICT, "repository_disabled")
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
            job, _created = await enqueue_repository_sync(
                session,
                repository=repository,
                source="admin",
                deduplication_key=f"repository:{repository_id}:sync:{secrets.token_hex(8)}",
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


def serialize_member(
    user: User,
    membership: ProjectMembership,
    identity: TelegramIdentity | None,
) -> dict[str, Any]:
    return {
        "project_id": str(membership.project_id),
        "user_id": str(user.id),
        "display_name": user.display_name,
        "telegram_user_id": identity.telegram_user_id if identity is not None else None,
        "telegram_username": identity.username if identity is not None else None,
        "role": membership.role,
        "department": membership.department,
        "stack": membership.stack,
        "language": membership.preferred_language,
        "knowledge_scope": membership.knowledge_scope,
        "can_create_requests": membership.can_create_requests,
        "active": user.active,
        "telegram_verified": identity is not None and identity.verified_at is not None,
        "telegram_reachable": identity is not None and identity.reachable,
    }


def serialize_agent_settings(
    row: ProjectAgentSettings | None,
    project_id: UUID,
) -> dict[str, Any]:
    if row is None:
        return {
            "project_id": str(project_id),
            "enabled": True,
            "claude_model": None,
            "claude_effort": "medium",
            "claude_timeout_seconds": 180,
            "max_budget_cents": None,
            "base_prompt": "",
            "answer_style": "normal",
            "privacy_level": "strict",
            "denied_globs": [],
            "memory_enabled": True,
            "memory_recent_messages": 24,
            "memory_max_context_chars": 24_000,
            "telegram_group_mode": "mentions",
            "telegram_private_mode": "all_messages",
            "telegram_streaming_enabled": True,
            "telegram_attach_markdown": True,
            "version": 0,
            "updated_by_admin_id": None,
            "created_at": None,
            "updated_at": None,
        }
    return {
        "project_id": str(row.project_id),
        "enabled": row.enabled,
        "claude_model": row.claude_model,
        "claude_effort": row.claude_effort,
        "claude_timeout_seconds": row.claude_timeout_seconds,
        "max_budget_cents": row.max_budget_cents,
        "base_prompt": row.base_prompt,
        "answer_style": row.answer_style,
        "privacy_level": row.privacy_level,
        "denied_globs": row.denied_globs,
        "memory_enabled": row.memory_enabled,
        "memory_recent_messages": row.memory_recent_messages,
        "memory_max_context_chars": row.memory_max_context_chars,
        "telegram_group_mode": row.telegram_group_mode,
        "telegram_private_mode": row.telegram_private_mode,
        "telegram_streaming_enabled": row.telegram_streaming_enabled,
        "telegram_attach_markdown": row.telegram_attach_markdown,
        "version": row.version,
        "updated_by_admin_id": (
            str(row.updated_by_admin_id) if row.updated_by_admin_id is not None else None
        ),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def claude_integration_status(
    settings: Settings,
    *,
    panel_configured: bool,
) -> dict[str, Any]:
    environment_configured = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip())
    source = "panel" if panel_configured else "environment" if environment_configured else "missing"
    return {
        "configured": source != "missing",
        "source": source,
        "proxy_configured": settings.outbound_proxy_url is not None,
    }


def validate_future_expiry(expires_at: datetime | None) -> None:
    if expires_at is None:
        return
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "expires_at_timezone_required")
    if expires_at <= utcnow():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "expires_at_must_be_future")


async def require_projects(session: Any, project_ids: list[UUID]) -> None:
    existing = set(await session.scalars(select(Project.id).where(Project.id.in_(project_ids))))
    if existing != set(project_ids):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project_not_found")


async def serialize_mcp_account(
    session: Any,
    account: ServiceAccount,
) -> dict[str, Any]:
    project_ids = list(
        await session.scalars(
            select(ServiceAccountProject.project_id)
            .where(ServiceAccountProject.service_account_id == account.id)
            .order_by(ServiceAccountProject.project_id)
        )
    )
    return {
        "id": str(account.id),
        "name": account.name,
        "active": account.active,
        "tool_scopes": account.tool_scopes,
        "project_ids": [str(project_id) for project_id in project_ids],
        "expires_at": account.expires_at,
        "last_used_at": account.last_used_at,
        "token_prefix": account.token_prefix,
        "version": account.version,
        "created_at": account.created_at,
        "updated_at": account.updated_at,
    }


def serialize_conversation_summary(
    row: ConversationThread,
    *,
    user_display_name: str | None,
    message_count: int,
    memory_count: int,
) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "chat_id": str(row.chat_id) if row.chat_id is not None else None,
        "user_id": str(row.user_id) if row.user_id is not None else None,
        "user_display_name": user_display_name,
        "message_count": message_count,
        "memory_count": memory_count,
        "last_message_at": row.last_message_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def serialize_conversation_message(row: ConversationMessage) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "role": row.role,
        "source": row.source,
        "content": row.content,
        "author_user_id": (str(row.author_user_id) if row.author_user_id is not None else None),
        "created_at": row.created_at,
    }


def serialize_conversation_memory(row: ConversationMemory) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "kind": row.kind,
        "memory_key": row.memory_key,
        "content": row.content,
        "updated_at": row.updated_at,
    }


def serialize_interaction(row: Interaction) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "repository_id": str(row.repository_id) if row.repository_id is not None else None,
        "conversation_thread_id": (
            str(row.conversation_thread_id) if row.conversation_thread_id is not None else None
        ),
        "correlation_id": row.correlation_id,
        "source": row.source,
        "source_ref": row.source_ref,
        "question": row.question,
        "commit_sha": row.commit_sha,
        "status": row.status,
        "answer_markdown": row.answer_markdown,
        "citations": row.citations,
        "rejected_citations": row.rejected_citations,
        "uncertainty": row.uncertainty,
        "provider_metadata": row.provider_metadata,
        "error_code": row.error_code,
        "artifacts": row.artifacts,
        "privacy_findings": row.privacy_findings,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def serialize_interaction_summary(row: Interaction) -> dict[str, Any]:
    question_limit = 500
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "repository_id": str(row.repository_id) if row.repository_id is not None else None,
        "conversation_thread_id": (
            str(row.conversation_thread_id) if row.conversation_thread_id is not None else None
        ),
        "source": row.source,
        "question": row.question[:question_limit],
        "question_truncated": len(row.question) > question_limit,
        "commit_sha": row.commit_sha,
        "status": row.status,
        "provider_metadata": row.provider_metadata,
        "error_code": row.error_code,
        "artifacts": [
            {
                key: artifact[key]
                for key in ("name", "filename", "kind", "media_type", "size_bytes")
                if key in artifact
            }
            for artifact in row.artifacts
        ],
        "privacy_findings_count": len(row.privacy_findings),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


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
        "created_by_user_id": (
            str(row.created_by_user_id) if row.created_by_user_id is not None else None
        ),
        "source_interaction_id": (
            str(row.source_interaction_id) if row.source_interaction_id is not None else None
        ),
        "correlation_id": row.correlation_id,
        "source": row.source,
        "requester_profile": row.requester_profile,
        "question": row.question,
        "agent_summary": row.agent_summary,
        "citations": row.citations,
        "kind": row.kind,
        "title": row.title,
        "description": row.description,
        "priority": row.priority,
        "status": row.status,
        "version": row.version,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def serialize_repository(row: Repository, settings: Settings) -> dict[str, Any]:
    safe_url = row.ssh_url.split("@", 1)[-1]
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "name": row.name,
        "ssh_url": safe_url,
        "default_branch": row.default_branch,
        "allowed_paths": row.allowed_paths,
        "github_repository": row.github_repository,
        "auto_sync_enabled": row.auto_sync_enabled,
        "auto_sync_mode": (
            "webhook_reconcile"
            if row.auto_sync_enabled and bool(settings.github_webhook_secret.get_secret_value())
            else "reconcile"
            if row.auto_sync_enabled
            else "disabled"
        ),
        "github_webhook_url": settings.github_webhook_url,
        "repository_reconcile_seconds": settings.repository_reconcile_seconds,
        "current_commit": row.current_commit,
        "status": row.status,
        "last_synced_at": row.last_synced_at,
        "last_webhook_at": row.last_webhook_at,
        "last_webhook_commit": row.last_webhook_commit,
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


def oauth_session_fingerprint(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()[:16]


def claude_oauth_http_exception(error: ClaudeError) -> HTTPException:
    status_code = {
        "claude_oauth_session_active": status.HTTP_409_CONFLICT,
        "claude_oauth_invalid_state": status.HTTP_409_CONFLICT,
        "claude_oauth_session_expired": status.HTTP_410_GONE,
        "claude_oauth_invalid_code": status.HTTP_422_UNPROCESSABLE_CONTENT,
        "claude_oauth_provider_error": status.HTTP_422_UNPROCESSABLE_CONTENT,
        "claude_oauth_proxy_required": status.HTTP_422_UNPROCESSABLE_CONTENT,
    }.get(error.code, status.HTTP_422_UNPROCESSABLE_CONTENT)
    return HTTPException(status_code, error.code)


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
