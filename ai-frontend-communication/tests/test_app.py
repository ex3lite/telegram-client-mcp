from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from itsdangerous import URLSafeTimedSerializer
from pydantic import SecretStr

from dca.app import create_app
from dca.config import Settings
from dca.db import AdminAccessKey, AdminPrincipal, AdminSession, AuditEvent
from dca.domain import utcnow
from dca.service import admin_key_fingerprint


@pytest.mark.asyncio
async def test_admin_dependency_is_a_cookie_not_a_query_parameter(tmp_path) -> None:
    app = create_app(
        Settings(
            session_secret=SecretStr("s" * 32),
            repository_root=tmp_path / "repositories",
            snapshot_root=tmp_path / "snapshots",
        )
    )

    operation = app.openapi()["paths"]["/api/v1/auth/me"]["get"]
    assert len(operation["parameters"]) == 1
    assert operation["parameters"][0]["name"] == "dca_admin"
    assert operation["parameters"][0]["in"] == "cookie"

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/api/v1/auth/me")
    finally:
        await app.state.telegram.close()
        await app.state.redis.aclose()
        await app.state.database.close()
    assert response.status_code == 401
    assert response.json() == {"detail": "authentication_required"}


@pytest.mark.asyncio
async def test_uuid_admin_login_cookie_and_immediate_revocation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server_secret = "test-session-secret-with-32-bytes"  # noqa: S105 - synthetic credential
    access_key = uuid4()
    principal = AdminPrincipal(id=uuid4(), name="Backend owner", active=True)
    key = AdminAccessKey(
        id=uuid4(),
        principal_id=principal.id,
        fingerprint=admin_key_fingerprint(access_key, server_secret),
        active=True,
    )
    added: list[object] = []

    class FakeSession:
        def add(self, value: object) -> None:
            added.append(value)

        async def flush(self) -> None:
            pass

        async def scalar(self, _: object) -> AdminAccessKey | None:
            return key

        async def get(self, model: object, object_id: object) -> object | None:
            if model is AdminSession:
                return next(
                    (
                        value
                        for value in added
                        if isinstance(value, AdminSession) and value.id == object_id
                    ),
                    None,
                )
            if model is AdminAccessKey and object_id == key.id:
                return key
            if model is AdminPrincipal and object_id == principal.id:
                return principal
            return None

    @asynccontextmanager
    async def session() -> AsyncIterator[FakeSession]:
        yield FakeSession()

    app = create_app(
        Settings(
            public_url="https://testserver",
            session_secret=SecretStr(server_secret),
            repository_root=tmp_path / "repositories",
            snapshot_root=tmp_path / "snapshots",
        )
    )
    monkeypatch.setattr(app.state.database, "session", session)
    monkeypatch.setattr(app.state.redis, "incr", AsyncMock(return_value=1))
    monkeypatch.setattr(app.state.redis, "expire", AsyncMock())

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="https://testserver") as client:
            rejected = await client.post(
                "/api/v1/auth/login",
                # The old email/password contract must stay rejected.
                json={"email": "admin@example.com", "password": "legacy"},
            )
            assert rejected.status_code == 422

            non_v4 = await client.post(
                "/api/v1/auth/login",
                json={"access_key": "00000000-0000-1000-8000-000000000001"},
            )
            assert non_v4.status_code == 422

            login = await client.post(
                "/api/v1/auth/login",
                json={"access_key": str(access_key)},
            )
            assert login.status_code == 200
            assert login.json() == {
                "principal_id": str(principal.id),
                "name": "Backend owner",
                "role": "owner",
            }
            cookie_header = login.headers["set-cookie"].lower()
            assert "max-age=15552000" in cookie_header
            assert "httponly" in cookie_header
            assert "secure" in cookie_header
            assert "samesite=lax" in cookie_header

            cookie = client.cookies.get("dca_admin")
            assert cookie is not None
            cookie_payload = URLSafeTimedSerializer(
                server_secret, salt="dca-admin-session-v2"
            ).loads(cookie)
            server_session = next(value for value in added if isinstance(value, AdminSession))
            assert cookie_payload == {"session_id": str(server_session.id)}
            assert str(access_key) not in cookie_payload.values()
            assert str(principal.id) not in cookie_payload.values()
            assert str(key.id) not in cookie_payload.values()
            assert not hasattr(key, "access_key")
            assert len(key.fingerprint) == 32
            assert server_session.access_key_id == key.id
            assert server_session.id.version == 4
            assert server_session.expires_at > utcnow()
            audit = next(value for value in added if isinstance(value, AuditEvent))
            assert isinstance(audit, AuditEvent)
            assert audit.event_type == "admin.login_succeeded"
            assert audit.actor_id == str(principal.id)
            assert audit.subject_id == str(key.id)
            assert audit.payload == {}

            me = await client.get("/api/v1/auth/me")
            assert me.status_code == 200
            expires_at = server_session.expires_at
            server_session.expires_at = utcnow() - timedelta(seconds=1)
            expired = await client.get("/api/v1/auth/me")
            assert expired.status_code == 401
            assert expired.json() == {"detail": "session_expired"}
            server_session.expires_at = expires_at
            key.active = False
            revoked = await client.get("/api/v1/auth/me")
            assert revoked.status_code == 401
            assert revoked.json() == {"detail": "session_revoked"}
            key.active = True
            principal.active = False
            principal_revoked = await client.get("/api/v1/auth/me")
            assert principal_revoked.status_code == 401
            assert principal_revoked.json() == {"detail": "session_revoked"}
            principal.active = True

            cross_origin = await client.post(
                "/api/v1/auth/logout",
                headers={"Origin": "https://attacker.invalid"},
            )
            assert cross_origin.status_code == 403
            assert server_session.revoked_at is None

            logout = await client.post(
                "/api/v1/auth/logout",
                headers={"Origin": "https://testserver"},
            )
            assert logout.status_code == 204
            assert server_session.revoked_at is not None
            assert client.cookies.get("dca_admin") is None

        async with AsyncClient(
            transport=transport,
            base_url="https://testserver",
            cookies={"dca_admin": cookie},
        ) as stolen_cookie_client:
            revoked = await stolen_cookie_client.get("/api/v1/auth/me")
            assert revoked.status_code == 401
            assert revoked.json() == {"detail": "session_revoked"}

            repeated_logout = await stolen_cookie_client.post(
                "/api/v1/auth/logout",
                headers={"Origin": "https://testserver"},
            )
            assert repeated_logout.status_code == 204
            assert "dca_admin=" in repeated_logout.headers["set-cookie"]

        async with AsyncClient(
            transport=transport,
            base_url="https://testserver",
            cookies={"dca_admin": "invalid-cookie"},
        ) as invalid_cookie_client:
            invalid_logout = await invalid_cookie_client.post(
                "/api/v1/auth/logout",
                headers={"Origin": "https://testserver"},
            )
            assert invalid_logout.status_code == 204
            assert "dca_admin=" in invalid_logout.headers["set-cookie"]

        audits = [value for value in added if isinstance(value, AuditEvent)]
        assert [audit.event_type for audit in audits] == [
            "admin.login_succeeded",
            "admin.logout",
        ]
    finally:
        await app.state.telegram.close()
        await app.state.redis.aclose()
        await app.state.database.close()


@pytest.mark.asyncio
async def test_root_frontend_does_not_bypass_mcp_auth(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frontend = tmp_path / "web" / "dist"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text("<!doctype html><title>Agency panel</title>")
    (frontend / "assets" / "app.js").write_text("export {}")
    monkeypatch.chdir(tmp_path)
    app = create_app(
        Settings(
            public_url="http://testserver",
            repository_root=tmp_path / "repositories",
            snapshot_root=tmp_path / "snapshots",
        )
    )
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            index = await client.get("/")
            asset = await client.get("/assets/app.js")
            metadata = await client.get("/.well-known/oauth-protected-resource/mcp")
            handshake = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "contract-test", "version": "1"},
                    },
                },
            )
    finally:
        await app.state.telegram.close()
        await app.state.redis.aclose()
        await app.state.database.close()

    assert index.status_code == 200
    assert "Agency panel" in index.text
    assert asset.status_code == 200
    assert metadata.status_code == 200
    assert metadata.json()["resource"] == "http://testserver/mcp"
    assert handshake.status_code == 401
    assert handshake.history == []


@pytest.mark.asyncio
async def test_unconfigured_admin_cannot_use_a_forged_fallback_cookie(tmp_path) -> None:
    app = create_app(
        Settings(
            repository_root=tmp_path / "repositories",
            snapshot_root=tmp_path / "snapshots",
        )
    )
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
            cookies={"dca_admin": "attacker-controlled"},
        ) as client:
            response = await client.get("/api/v1/auth/me")
    finally:
        await app.state.telegram.close()
        await app.state.redis.aclose()
        await app.state.database.close()

    assert response.status_code == 503
    assert response.json() == {"detail": "admin_auth_not_configured"}
