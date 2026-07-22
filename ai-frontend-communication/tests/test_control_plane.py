from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from dca.app import (
    AgentSettingsInput,
    McpAccountCreateInput,
    McpAccountPatchInput,
    MemberUpdateInput,
    RepositoryScopeInput,
    _purge_claude_session_artifacts,
    claude_integration_status,
    create_app,
    serialize_interaction_summary,
    serialize_mcp_account,
    serialize_member,
    serialize_request,
)
from dca.config import Settings
from dca.db import (
    AgentMessage,
    ChangeRequest,
    Interaction,
    ProjectAgentSettings,
    ProjectMembership,
    ServiceAccount,
    TelegramIdentity,
    User,
)
from dca.mcp import MCP_TOOL_SCOPES
from dca.service import (
    ServiceError,
    create_agent_message,
    decrypt_system_secret,
    encrypt_system_secret,
    load_project_agent_settings,
    validate_agent_message,
)


def valid_agent_settings() -> dict[str, object]:
    return {
        "expected_version": 0,
        "enabled": True,
        "claude_model": None,
        "claude_effort": "medium",
        "claude_timeout_seconds": 180,
        "max_budget_cents": None,
        "base_prompt": "",
        "answer_style": "normal",
        "privacy_level": "strict",
        "denied_globs": [".env", "**/*.pem"],
        "memory_enabled": True,
        "memory_recent_messages": 24,
        "memory_max_context_chars": 24_000,
        "telegram_group_mode": "mentions",
        "telegram_private_mode": "all_messages",
        "telegram_streaming_enabled": True,
        "telegram_attach_markdown": True,
    }


def test_repository_scope_normalizes_and_rejects_escape_paths() -> None:
    payload = RepositoryScopeInput(allowed_paths=[" src/api ", "docs", "src/api"])

    assert payload.allowed_paths == ["docs", "src/api"]
    for invalid in (["../etc"], ["/etc"], ["src\\private"]):
        with pytest.raises(ValidationError):
            RepositoryScopeInput(allowed_paths=invalid)


def test_system_secret_encryption_is_authenticated_and_key_scoped() -> None:
    server_secret = "s" * 32
    plaintext = "oauth-token-that-must-not-be-stored"

    ciphertext = encrypt_system_secret(plaintext, server_secret)

    assert plaintext.encode() not in ciphertext
    assert decrypt_system_secret(ciphertext, server_secret) == plaintext
    with pytest.raises(ServiceError, match="cannot be decrypted"):
        decrypt_system_secret(ciphertext, "x" * 32)


@pytest.mark.parametrize(
    "text_markdown, attachment_name, attachment_markdown, expected_kind, expected_location",
    [
        (
            "Bearer abcdefghijklmnopqrstuvwxyz",
            None,
            None,
            "bearer_token",
            "agent_message.text",
        ),
        (
            "Safe message",
            "report.md",
            "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz123456",
            "openai_token",
            "agent_message.attachment",
        ),
    ],
)
@pytest.mark.asyncio
async def test_strict_privacy_blocks_mcp_message_before_persistence(
    text_markdown: str,
    attachment_name: str | None,
    attachment_markdown: str | None,
    expected_kind: str,
    expected_location: str,
) -> None:
    project_id = uuid4()
    account_id = uuid4()
    account = ServiceAccount(
        id=account_id,
        name="agent",
        token_prefix=uuid4().hex[:8],
        token_hash=uuid4().hex,
        tool_scopes=["telegram.send_message"],
    )
    settings = ProjectAgentSettings(
        project_id=project_id,
        enabled=True,
        privacy_level="strict",
        version=1,
    )

    class StrictSession:
        scalar_calls = 0
        execute_calls = 0

        async def scalar(self, _: object) -> ServiceAccount:
            self.scalar_calls += 1
            return account

        async def get(self, model: object, _: object) -> object | None:
            if model is ProjectAgentSettings:
                return settings
            return None

        async def execute(self, _: object) -> None:
            self.execute_calls += 1
            raise AssertionError("strict privacy must reject before INSERT")

    session = StrictSession()
    with pytest.raises(ServiceError) as error:
        await create_agent_message(
            session,  # type: ignore[arg-type]
            service_account_id=account_id,
            project_id=project_id,
            correlation_id="run-1",
            idempotency_key="message-1",
            target_user_id=uuid4(),
            target_chat_id=None,
            text_markdown=text_markdown,
            attachment_name=attachment_name,
            attachment_markdown=attachment_markdown,
        )

    assert error.value.code == "privacy_blocked"
    assert error.value.metadata == {
        "privacy_findings_count": 1,
        "privacy_findings": [{"kind": expected_kind, "location": expected_location}],
    }
    assert "abcdefghijklmnopqrstuvwxyz" not in str(error.value.metadata)
    assert session.scalar_calls == 1
    assert session.execute_calls == 0


@pytest.mark.asyncio
async def test_balanced_privacy_persists_only_redacted_agent_message() -> None:
    project_id = uuid4()
    account_id = uuid4()
    target_user_id = uuid4()
    account = ServiceAccount(
        id=account_id,
        name="agent",
        token_prefix=uuid4().hex[:8],
        token_hash=uuid4().hex,
        tool_scopes=["telegram.send_message"],
    )
    settings = ProjectAgentSettings(
        project_id=project_id,
        enabled=True,
        privacy_level="balanced",
        version=1,
    )

    class InsertResult:
        def __init__(self, inserted_id: object) -> None:
            self.inserted_id = inserted_id

        def scalar_one_or_none(self) -> object:
            return self.inserted_id

    class BalancedSession:
        def __init__(self) -> None:
            self.scalar_calls = 0
            self.inserted_values: dict[str, object] = {}
            self.message: AgentMessage | None = None

        async def scalar(self, _: object) -> object | None:
            self.scalar_calls += 1
            if self.scalar_calls == 1:
                return account
            if self.scalar_calls == 2:
                return None
            return object()

        async def get(self, model: object, _: object) -> object | None:
            if model is ProjectAgentSettings:
                return settings
            if model is AgentMessage:
                return self.message
            return None

        async def execute(self, statement: object) -> InsertResult:
            self.inserted_values = statement.compile().params  # type: ignore[union-attr]
            self.message = AgentMessage(**self.inserted_values)
            return InsertResult(self.inserted_values["id"])

        def add(self, _: object) -> None:
            return None

        async def flush(self) -> None:
            return None

    session = BalancedSession()
    raw_text = "Bearer abcdefghijklmnopqrstuvwxyz"
    raw_attachment = "sk-proj-abcdefghijklmnopqrstuvwxyz123456"
    message, created = await create_agent_message(
        session,  # type: ignore[arg-type]
        service_account_id=account_id,
        project_id=project_id,
        correlation_id="run-1",
        idempotency_key="message-balanced-1",
        target_user_id=target_user_id,
        target_chat_id=None,
        text_markdown=f"Text {raw_text}",
        attachment_name="report.md",
        attachment_markdown=f"Token {raw_attachment}",
    )

    assert created is True
    assert message.text_markdown == "Text [REDACTED:bearer_token]"
    assert message.attachment_markdown == "Token [REDACTED:openai_token]"
    assert message.privacy_findings == [
        {
            "kind": "bearer_token",
            "location": "agent_message.text",
            "action": "redacted",
        },
        {
            "kind": "openai_token",
            "location": "agent_message.attachment",
            "action": "redacted",
        },
    ]
    persisted = str(session.inserted_values)
    assert raw_text not in persisted
    assert raw_attachment not in persisted


@pytest.mark.asyncio
async def test_missing_agent_settings_use_secure_runtime_defaults() -> None:
    project_id = uuid4()

    class EmptySession:
        async def get(self, _: object, __: object) -> None:
            return None

    settings = await load_project_agent_settings(EmptySession(), project_id)  # type: ignore[arg-type]

    assert isinstance(settings, ProjectAgentSettings)
    assert settings.project_id == project_id
    assert settings.version == 0
    assert settings.privacy_level == "strict"
    assert settings.memory_enabled is True
    assert settings.memory_recent_messages == 24
    assert settings.memory_max_context_chars == 24_000
    assert settings.telegram_group_mode == "mentions"
    assert settings.telegram_private_mode == "all_messages"
    assert settings.telegram_streaming_enabled is True


def test_control_plane_inputs_reject_unsafe_values() -> None:
    valid = valid_agent_settings()
    assert AgentSettingsInput.model_validate(valid).privacy_level == "strict"

    with pytest.raises(ValidationError):
        AgentSettingsInput.model_validate({**valid, "claude_timeout_seconds": 901})
    with pytest.raises(ValidationError):
        AgentSettingsInput.model_validate({**valid, "denied_globs": [".env", ".env"]})
    with pytest.raises(ValidationError):
        AgentSettingsInput.model_validate({**valid, "memory_recent_messages": 101})
    with pytest.raises(ValidationError):
        AgentSettingsInput.model_validate({**valid, "memory_max_context_chars": 2_999})
    with pytest.raises(ValidationError):
        AgentSettingsInput.model_validate(
            {**valid, "base_prompt": "Use OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz123456"}
        )
    with pytest.raises(ValidationError):
        McpAccountCreateInput(
            name="agent",
            tool_scopes=["filesystem.delete_everything"],
            project_ids=[uuid4()],
        )
    assert "telegram.ask_user" in MCP_TOOL_SCOPES


def test_member_input_normalizes_profile_and_rejects_untrusted_enums() -> None:
    values = {
        "display_name": "  Бека  ",
        "telegram_user_id": 1_118_192_318,
        "telegram_username": " @beka_android ",
        "role": " android ",
        "department": " Mobile ",
        "stack": " Kotlin ",
        "language": "ru",
        "knowledge_scope": "integration",
        "can_create_requests": True,
        "active": True,
    }

    profile = MemberUpdateInput.model_validate(values)

    assert profile.display_name == "Бека"
    assert profile.telegram_username == "beka_android"
    assert profile.role == "android"
    assert profile.department == "Mobile"
    assert profile.stack == "Kotlin"
    with pytest.raises(ValidationError):
        MemberUpdateInput.model_validate({**values, "language": "auto"})
    with pytest.raises(ValidationError):
        MemberUpdateInput.model_validate({**values, "knowledge_scope": "everything"})
    with pytest.raises(ValidationError):
        MemberUpdateInput.model_validate({**values, "telegram_username": "bad username"})


def test_member_serialization_combines_user_membership_and_telegram_identity() -> None:
    user_id = uuid4()
    project_id = uuid4()
    user = User(id=user_id, display_name="Игорь", active=True)
    membership = ProjectMembership(
        project_id=project_id,
        user_id=user_id,
        role="ios",
        department="Mobile",
        stack="Swift",
        preferred_language="ru",
        knowledge_scope="integration",
        can_create_requests=True,
    )
    identity = TelegramIdentity(
        user_id=user_id,
        telegram_user_id=719_969_066,
        username="igor_ios",
        verified_at=datetime.now(UTC),
        reachable=True,
    )

    result = serialize_member(user, membership, identity)

    assert result == {
        "project_id": str(project_id),
        "user_id": str(user_id),
        "display_name": "Игорь",
        "telegram_user_id": 719_969_066,
        "telegram_username": "igor_ios",
        "role": "ios",
        "department": "Mobile",
        "stack": "Swift",
        "language": "ru",
        "knowledge_scope": "integration",
        "can_create_requests": True,
        "active": True,
        "telegram_verified": True,
        "telegram_reachable": True,
    }


def test_claude_session_purge_deletes_only_exact_uuid_artifacts(tmp_path: Path) -> None:
    session_id = uuid4()
    root = tmp_path / "claude-sessions"
    project = root / "projects" / "snapshot"
    project.mkdir(parents=True)
    transcript = project / f"{session_id}.jsonl"
    transcript.write_text("transcript")
    session_directory = root / "session-env" / str(session_id)
    session_directory.mkdir(parents=True)
    (session_directory / "environment").write_text("safe")
    decoy = project / f"prefix-{session_id}.jsonl"
    decoy.write_text("keep")
    backup = project / f"{session_id}.jsonl.backup"
    backup.write_text("keep")

    assert _purge_claude_session_artifacts(root, session_id) == 2
    assert not transcript.exists()
    assert not session_directory.exists()
    assert decoy.read_text() == "keep"
    assert backup.read_text() == "keep"


def test_claude_session_purge_fails_closed_for_exact_name_symlink(tmp_path: Path) -> None:
    session_id = uuid4()
    root = tmp_path / "claude-sessions"
    project = root / "projects" / "snapshot"
    project.mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    outside.write_text("keep")
    (project / f"{session_id}.jsonl").symlink_to(outside)
    legitimate = root / "session-env" / str(session_id)
    legitimate.mkdir(parents=True)
    (legitimate / "environment").write_text("keep")

    with pytest.raises(RuntimeError, match="symlink"):
        _purge_claude_session_artifacts(root, session_id)

    assert outside.read_text() == "keep"
    assert legitimate.is_dir()


@pytest.mark.parametrize(
    "unsafe_glob",
    [
        "/etc/passwd",
        ":(glob)**/.env",
        "../.env",
        "secrets/../.env",
        r"secrets\*.env",
        "секреты/*.env",
    ],
)
def test_agent_settings_reject_runtime_unsafe_denied_globs(unsafe_glob: str) -> None:
    with pytest.raises(ValidationError):
        AgentSettingsInput.model_validate({**valid_agent_settings(), "denied_globs": [unsafe_glob]})


def test_markdown_attachment_limits_telegram_caption_length() -> None:
    with pytest.raises(ServiceError) as error:
        validate_agent_message(
            correlation_id="run-1",
            idempotency_key="message-1",
            target_user_id=uuid4(),
            target_chat_id=None,
            text_markdown="x" * 1_025,
            attachment_name="answer.md",
            attachment_markdown="# Full answer",
        )

    assert error.value.code == "invalid_message"


@pytest.mark.parametrize(
    "unsafe_name",
    [
        ".hidden.md",
        "foo..bar.md",
        "отчёт.md",
        "answer.MD",
        "folder/answer.md",
        r"folder\answer.md",
        "-answer.md",
        "answer.txt",
    ],
)
def test_agent_message_rejects_attachment_names_telegram_cannot_deliver(
    unsafe_name: str,
) -> None:
    with pytest.raises(ServiceError) as error:
        validate_agent_message(
            correlation_id="run-1",
            idempotency_key="message-1",
            target_user_id=uuid4(),
            target_chat_id=None,
            text_markdown="See attachment",
            attachment_name=unsafe_name,
            attachment_markdown="# Full answer",
        )

    assert error.value.code == "invalid_attachment"


def test_mcp_patch_can_explicitly_clear_expiry() -> None:
    payload = McpAccountPatchInput(expected_version=1, expires_at=None)

    assert "expires_at" in payload.model_fields_set
    assert payload.expires_at is None


@pytest.mark.asyncio
async def test_mcp_account_serialization_exposes_metadata_not_hash() -> None:
    project_id = uuid4()
    account = ServiceAccount(
        id=uuid4(),
        name="codex-production",
        token_prefix=uuid4().hex[:8],
        token_hash=uuid4().hex,
        tool_scopes=["telegram.ask_user"],
        active=True,
        version=3,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    class AccountSession:
        async def scalars(self, _: object) -> list[object]:
            return [project_id]

    result = await serialize_mcp_account(AccountSession(), account)

    assert result["id"] == str(account.id)
    assert result["project_ids"] == [str(project_id)]
    assert result["tool_scopes"] == ["telegram.ask_user"]
    assert result["version"] == 3
    assert "token_hash" not in result
    assert "token" not in result


def test_interaction_summary_omits_heavy_and_sensitive_fields() -> None:
    interaction = Interaction(
        id=uuid4(),
        project_id=uuid4(),
        correlation_id="test",
        source="telegram",
        source_ref={"requester_profile": {"secret": "not-for-list"}},
        question="q" * 700,
        status="answer_ready",
        answer_markdown="large answer",
        citations=[],
        rejected_citations=[],
        uncertainty=[],
        provider_metadata={"provider": "claude-code-cli"},
        artifacts=[
            {
                "name": "answer.md",
                "media_type": "text/markdown",
                "size_bytes": 12,
                "content": "sensitive body",
                "path": "/private/runtime/answer.md",
            }
        ],
        privacy_findings=[{"kind": "secret", "location": "answer"}],
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    result = serialize_interaction_summary(interaction)

    assert result["question"] == "q" * 500
    assert result["question_truncated"] is True
    assert result["artifacts"] == [
        {"name": "answer.md", "media_type": "text/markdown", "size_bytes": 12}
    ]
    assert result["privacy_findings_count"] == 1
    assert "answer_markdown" not in result
    assert "source_ref" not in result
    assert "privacy_findings" not in result


def test_request_serialization_exposes_safe_inbox_context() -> None:
    user_id = uuid4()
    interaction_id = uuid4()
    request = ChangeRequest(
        id=uuid4(),
        project_id=uuid4(),
        created_by_user_id=user_id,
        source_interaction_id=interaction_id,
        correlation_id="tg:42",
        source="agent",
        source_ref={"delivery": {"chat_id": 123}},
        requester_profile={
            "display_name": "Бека",
            "department": "Mobile",
            "stack": "Android / Kotlin",
            "language": "ru",
        },
        question="Как внедрить аватарки?",
        agent_summary="Нужен публичный контракт API.",
        citations=[{"path": "src/avatar.py", "start_line": 10, "end_line": 20}],
        kind="integration",
        title="Контракт API аватарок",
        description="",
        priority="normal",
        status="open",
        version=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    result = serialize_request(request)

    assert result["created_by_user_id"] == str(user_id)
    assert result["source_interaction_id"] == str(interaction_id)
    assert result["requester_profile"]["language"] == "ru"
    assert result["question"] == "Как внедрить аватарки?"
    assert result["citations"][0]["path"] == "src/avatar.py"
    assert "source_ref" not in result


def test_claude_integration_status_never_exposes_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        session_secret=SecretStr("s" * 32),
        outbound_proxy_url="http://127.0.0.1:8080",
    )
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "environment-secret")

    environment = claude_integration_status(settings, panel_configured=False)
    panel = claude_integration_status(settings, panel_configured=True)

    assert environment == {
        "configured": True,
        "source": "environment",
        "proxy_configured": True,
    }
    assert panel["source"] == "panel"
    assert "token" not in environment
    assert "token" not in panel


@pytest.mark.asyncio
async def test_control_plane_routes_are_registered(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            session_secret=SecretStr("s" * 32),
            repository_root=tmp_path / "repositories",
            snapshot_root=tmp_path / "snapshots",
        )
    )
    try:
        paths = app.openapi()["paths"]
        assert set(paths["/api/v1/members"]) == {"get"}
        assert set(paths["/api/v1/projects/{project_id}/members/{user_id}"]) == {"put"}
        assert set(paths["/api/v1/projects/{project_id}/agent-settings"]) == {"get", "put"}
        assert set(paths["/api/v1/repositories/{repository_id}/scope"]) == {"put"}
        assert set(paths["/api/v1/integrations/claude"]) == {"get", "put", "delete"}
        assert set(paths["/api/v1/integrations/claude/check"]) == {"post"}
        assert set(paths["/api/v1/integrations/claude/oauth/start"]) == {"post"}
        assert set(paths["/api/v1/integrations/claude/oauth/complete"]) == {"post"}
        assert set(paths["/api/v1/integrations/claude/oauth/{session_id}"]) == {"delete"}
        assert set(paths["/api/v1/mcp/accounts"]) == {"get", "post"}
        assert set(paths["/api/v1/mcp/accounts/{account_id}"]) == {"patch"}
        assert set(paths["/api/v1/mcp/accounts/{account_id}/rotate-token"]) == {"post"}
        assert set(paths["/api/v1/interactions"]) == {"get"}
        assert set(paths["/api/v1/interactions/{interaction_id}"]) == {"get"}
        assert set(paths["/api/v1/conversations"]) == {"get"}
        assert set(paths["/api/v1/conversations/{thread_id}"]) == {"get", "delete"}
    finally:
        await app.state.telegram.close()
        await app.state.redis.aclose()
        await app.state.database.close()
