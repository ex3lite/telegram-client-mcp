from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from itsdangerous import URLSafeTimedSerializer
from pydantic import SecretStr

from dca.app import create_app
from dca.claude import ClaudeCode, ClaudeError, ClaudeOAuthManager, ClaudeOAuthStart
from dca.config import Settings
from dca.db import (
    AdminAccessKey,
    AdminPrincipal,
    AdminSession,
    AuditEvent,
    SystemSecret,
)
from dca.domain import utcnow
from dca.service import decrypt_system_secret


def write_fake_setup_token_cli(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import os
import sys
import time
import tty

blocked = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
if any(name in os.environ for name in blocked):
    print("credential environment leaked", flush=True)
    raise SystemExit(2)
if os.environ.get("HTTP_PROXY") != "http://proxy.example:8080/":
    print("proxy missing", flush=True)
    raise SystemExit(3)
if os.environ.get("HTTPS_PROXY") != "http://proxy.example:8080/":
    print("proxy missing", flush=True)
    raise SystemExit(4)

print("Browser didn't open?\\033[23GUse the url\\033[35Gbelow\\033[41Gto\\033[44Gsign\\033[49Gin")
print(
    "\\033]8;id=test-link;https://claude.ai/oauth/authorize?state=test-state\\033\\\\"
    "Open URL\\033]8;;\\033\\\\"
)
print("Paste\\033[8Gcode\\033[13Ghere\\033[18Gif\\033[21Gprompted\\033[30G>", flush=True)
tty.setraw(sys.stdin.fileno())
time.sleep(0.05)
first_input = os.read(sys.stdin.fileno(), 4096)
if b"\\r" in first_input:
    print("Code and Enter were coalesced", flush=True)
    raise SystemExit(6)
code_bytes = bytearray(first_input)
while True:
    character = os.read(sys.stdin.fileno(), 1)
    if character == b"\\r":
        break
    code_bytes.extend(character)
code = code_bytes.decode()
if code not in {"one-time-code", "invalid-provider-token"}:
    print("Invalid code", flush=True)
    raise SystemExit(5)
print("Paste code here if prompted >", flush=True)
time.sleep(0.05)
print(
    "Your\\033[6GOAuth\\033[12Gtoken\\033[18G(valid\\033[25Gfor"
    "\\033[29G1\\033[31Gyear):",
    flush=True,
)
print("----------------------------------------", flush=True)
print("Use this token by setting: export CLAUDE_CODE_OAUTH_TOKEN=<token>", flush=True)
time.sleep(0.05)
token = (
    "sk-ant-oat01-abcdefghijklmnopqrstuvwxyz1234567890"
    if code == "one-time-code"
    else "long-lived-but-not-a-setup-token-value"
)
print(token, flush=True)
"""
    )
    path.chmod(0o700)


@pytest.mark.asyncio
async def test_claude_oauth_pty_flow_and_terminal_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "fake-claude"
    write_fake_setup_token_cli(executable)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "must-not-leak")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "must-not-leak")
    settings = Settings(
        claude_bin=str(executable),
        outbound_proxy_url="http://proxy.example:8080",
    )
    manager = ClaudeOAuthManager(
        settings,
        ttl_seconds=10,
        start_timeout_seconds=3,
        complete_timeout_seconds=3,
    )
    restarted = ClaudeOAuthManager(settings)
    owner_id = uuid4()
    other_owner_id = uuid4()
    try:
        started = await manager.start(owner_id)
        assert started.authorization_url == ("https://claude.ai/oauth/authorize?state=test-state")
        assert 32 <= len(started.session_id) <= 128

        process = manager._sessions[started.session_id].process
        repeated = await manager.start(owner_id)
        assert repeated == started
        assert manager._sessions[started.session_id].process is process
        assert len(manager._sessions) == 1

        other = await manager.start(other_owner_id)
        assert other.session_id != started.session_id
        assert len(manager._sessions) == 2

        missing_session_id = "x" * 43
        with pytest.raises(ClaudeError) as wrong_owner_state:
            await manager.complete(other_owner_id, started.session_id, "one-time-code")
        with pytest.raises(ClaudeError) as missing_state:
            await manager.complete(other_owner_id, missing_session_id, "one-time-code")
        assert (wrong_owner_state.value.code, wrong_owner_state.value.message) == (
            missing_state.value.code,
            missing_state.value.message,
        )
        assert manager._sessions[started.session_id].state == "awaiting_code"
        assert await manager.cancel(other_owner_id, started.session_id) is False
        assert manager._sessions[started.session_id].state == "awaiting_code"

        issued_value = await manager.complete(owner_id, started.session_id, "one-time-code")
        assert issued_value == "sk-ant-oat01-abcdefghijklmnopqrstuvwxyz1234567890"
        assert await manager.cancel(other_owner_id, other.session_id) is True

        invalid_provider = await manager.start(owner_id)
        with pytest.raises(ClaudeError) as invalid_token:
            await manager.complete(owner_id, invalid_provider.session_id, "invalid-provider-token")
        assert invalid_token.value.code == "claude_oauth_provider_error"

        with pytest.raises(ClaudeError) as restart_state:
            await restarted.complete(owner_id, started.session_id, "one-time-code")
        assert restart_state.value.code == "claude_oauth_invalid_state"

        cancelled = await manager.start(owner_id)
        assert await manager.cancel(owner_id, cancelled.session_id) is True
        assert await manager.cancel(owner_id, cancelled.session_id) is False
        with pytest.raises(ClaudeError) as cancelled_state:
            await manager.complete(owner_id, cancelled.session_id, "one-time-code")
        assert cancelled_state.value.code == "claude_oauth_invalid_state"

        expired = await manager.start(owner_id)
        manager._sessions[expired.session_id].expires_at = utcnow() - timedelta(seconds=1)
        with pytest.raises(ClaudeError) as expired_state:
            await manager.complete(owner_id, expired.session_id, "one-time-code")
        assert expired_state.value.code == "claude_oauth_session_expired"
    finally:
        await manager.close()
        await restarted.close()


@pytest.mark.asyncio
async def test_claude_oauth_api_encrypts_value_and_never_returns_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server_secret = "test-session-secret-with-at-least-32-bytes"  # noqa: S105
    principal = AdminPrincipal(id=uuid4(), name="Owner", active=True)
    key = AdminAccessKey(
        id=uuid4(),
        principal_id=principal.id,
        fingerprint=b"k" * 32,
        active=True,
    )
    admin_session = AdminSession(
        id=uuid4(),
        access_key_id=key.id,
        expires_at=utcnow() + timedelta(hours=1),
    )
    added: list[object] = []

    class FakeSession:
        async def get(self, model: object, object_id: object) -> object | None:
            if model is AdminSession and object_id == admin_session.id:
                return admin_session
            if model is AdminAccessKey and object_id == key.id:
                return key
            if model is AdminPrincipal and object_id == principal.id:
                return principal
            if model is SystemSecret:
                return next((item for item in added if isinstance(item, SystemSecret)), None)
            return None

        def add(self, value: object) -> None:
            added.append(value)

        async def flush(self) -> None:
            return None

    @asynccontextmanager
    async def session() -> AsyncIterator[FakeSession]:
        yield FakeSession()

    app = create_app(
        Settings(
            public_url="https://testserver",
            session_secret=SecretStr(server_secret),
            outbound_proxy_url="http://proxy.example:8080",
            repository_root=tmp_path / "repositories",
            snapshot_root=tmp_path / "snapshots",
        )
    )
    monkeypatch.setattr(app.state.database, "session", session)
    session_id = "s" * 43
    start = AsyncMock(
        return_value=ClaudeOAuthStart(
            session_id=session_id,
            authorization_url="https://claude.ai/oauth/authorize?state=test-state",
            expires_at=utcnow() + timedelta(minutes=10),
        )
    )
    issued_value = "sk-ant-oat01-abcdefghijklmnopqrstuvwxyz1234567890"
    complete = AsyncMock(return_value=issued_value)
    cancel = AsyncMock(return_value=True)
    monkeypatch.setattr(app.state.claude_oauth, "start", start)
    monkeypatch.setattr(app.state.claude_oauth, "complete", complete)
    monkeypatch.setattr(app.state.claude_oauth, "cancel", cancel)
    cookie = URLSafeTimedSerializer(server_secret, salt="dca-admin-session-v2").dumps(
        {"session_id": str(admin_session.id)}
    )
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(
            transport=transport,
            base_url="https://testserver",
            cookies={"dca_admin": cookie},
        ) as client:
            invalid_manual = await client.put(
                "/api/v1/integrations/claude",
                json={"oauth_token": "authorization-code-value#oauth-state"},
                headers={"Origin": "https://testserver"},
            )
            assert invalid_manual.status_code == 422
            assert invalid_manual.json()["detail"] == "claude_oauth_invalid_token"
            assert not any(isinstance(item, SystemSecret) for item in added)

            cross_origin = await client.post(
                "/api/v1/integrations/claude/oauth/start",
                json={},
                headers={"Origin": "https://attacker.invalid"},
            )
            assert cross_origin.status_code == 403
            start.assert_not_awaited()

            started = await client.post(
                "/api/v1/integrations/claude/oauth/start",
                json={},
                headers={"Origin": "https://testserver"},
            )
            assert started.status_code == 200
            assert started.json()["session_id"] == session_id
            assert issued_value not in started.text
            start.assert_awaited_once_with(principal.id)

            completed = await client.post(
                "/api/v1/integrations/claude/oauth/complete",
                json={"session_id": session_id, "code": "one-time-code"},
                headers={"Origin": "https://testserver"},
            )
            assert completed.status_code == 200
            assert completed.json() == {
                "configured": True,
                "source": "panel",
                "proxy_configured": True,
            }
            assert issued_value not in completed.text
            complete.assert_awaited_once_with(principal.id, session_id, "one-time-code")

            async def reject_probe(_: ClaudeCode, oauth_token: str | None = None) -> str:
                assert oauth_token == issued_value
                raise ClaudeError(
                    "model_provider_authentication_failed",
                    "provider detail that must not be returned",
                )

            monkeypatch.setattr(ClaudeCode, "probe", reject_probe)
            checked = await client.post(
                "/api/v1/integrations/claude/check",
                headers={"Origin": "https://testserver"},
            )
            assert checked.status_code == 200
            assert checked.json() == {
                "ok": False,
                "version": None,
                "error_code": "model_provider_authentication_failed",
            }
            assert "provider detail" not in checked.text

            invalid_cancel = await client.delete(
                "/api/v1/integrations/claude/oauth/short",
                headers={"Origin": "https://testserver"},
            )
            assert invalid_cancel.status_code == 422
            cancelled = await client.delete(
                f"/api/v1/integrations/claude/oauth/{session_id}",
                headers={"Origin": "https://testserver"},
            )
            assert cancelled.status_code == 204
            cancel.assert_awaited_once_with(principal.id, session_id)

        stored = next(item for item in added if isinstance(item, SystemSecret))
        assert issued_value.encode() not in stored.ciphertext
        assert decrypt_system_secret(stored.ciphertext, server_secret) == issued_value
        audits = [item for item in added if isinstance(item, AuditEvent)]
        assert [event.event_type for event in audits] == [
            "claude.oauth_started",
            "claude.oauth_completed",
            "claude.integration_checked",
            "claude.oauth_cancelled",
        ]
        assert issued_value not in str([event.payload for event in audits])
    finally:
        await app.state.claude_oauth.close()
        await app.state.telegram.close()
        await app.state.redis.aclose()
        await app.state.database.close()
