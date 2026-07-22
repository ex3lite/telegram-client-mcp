import os
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

import dca.bootstrap as bootstrap_module
from dca.bootstrap import _repository_credential_file_is_secure, build_parser
from dca.config import Settings


def test_link_chat_cli_contract() -> None:
    args = build_parser().parse_args(
        [
            "link-chat",
            "--project-slug",
            "backend",
            "--telegram-chat-id",
            "-1001234567890",
        ]
    )

    assert args.project_slug == "backend"
    assert args.telegram_chat_id == -1001234567890
    assert args.message_thread_id is None
    assert args.kind == "group"


def test_link_user_accepts_project_profile() -> None:
    args = build_parser().parse_args(
        [
            "link-user",
            "--project-slug",
            "backend",
            "--name",
            "Бека",
            "--telegram-user-id",
            "1118192318",
            "--department",
            "Mobile",
            "--stack",
            "Android / Kotlin",
            "--preferred-language",
            "ru",
        ]
    )

    assert args.department == "Mobile"
    assert args.stack == "Android / Kotlin"
    assert args.preferred_language == "ru"
    assert args.role is None


def test_existing_repository_can_enable_github_auto_sync() -> None:
    args = build_parser().parse_args(
        [
            "repository-auto-sync",
            "--project-slug",
            "backend",
            "--name",
            "backend_ai",
            "--github-repository",
            "Matrena-VPN/backend_ai",
        ]
    )

    assert args.project_slug == "backend"
    assert args.name == "backend_ai"
    assert args.github_repository == "Matrena-VPN/backend_ai"
    assert args.disable is False


def test_repository_credentials_require_root_service_group_0640() -> None:
    def metadata(mode: int, uid: int, gid: int) -> os.stat_result:
        return os.stat_result((stat.S_IFREG | mode, 0, 0, 1, uid, gid, 0, 0, 0, 0))

    assert _repository_credential_file_is_secure(metadata(0o640, 0, 987), service_gid=987)
    assert not _repository_credential_file_is_secure(metadata(0o600, 0, 0), service_gid=987)
    assert not _repository_credential_file_is_secure(metadata(0o644, 0, 987), service_gid=987)


def test_admin_key_cli_accepts_optional_uuid() -> None:
    args = build_parser().parse_args(
        [
            "admin-key",
            "--name",
            "Backend owner",
            "--uuid",
            "11111111-2222-4333-8444-555555555555",
        ]
    )

    assert args.name == "Backend owner"
    assert args.access_key == UUID("11111111-2222-4333-8444-555555555555")


@pytest.mark.parametrize(
    "access_key",
    [
        "00000000-0000-0000-0000-000000000000",
        "00000000-0000-1000-8000-000000000001",
    ],
)
def test_admin_key_cli_rejects_non_uuid4(access_key: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["admin-key", "--name", "Backend owner", "--uuid", access_key])


def test_admin_key_revoke_cli_accepts_name_or_internal_key_id() -> None:
    by_name = build_parser().parse_args(["admin-key-revoke", "--name", "Backend owner"])
    by_key = build_parser().parse_args(
        [
            "admin-key-revoke",
            "--key-id",
            "11111111-2222-4333-8444-555555555555",
        ]
    )

    assert by_name.name == "Backend owner"
    assert by_name.key_id is None
    assert by_key.name is None
    assert by_key.key_id == UUID("11111111-2222-4333-8444-555555555555")


@pytest.mark.asyncio
async def test_telegram_setup_polling_deletes_webhook_without_dropping_updates(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bot = SimpleNamespace(
        get_me=AsyncMock(
            return_value=SimpleNamespace(
                username="dca_bot",
                has_topics_enabled=True,
                supports_guest_queries=True,
            )
        ),
        delete_webhook=AsyncMock(return_value=True),
        set_webhook=AsyncMock(return_value=True),
    )
    adapter = SimpleNamespace(
        bot=bot,
        setup_commands=AsyncMock(),
        allowed_updates=lambda: ["message"],
        close=AsyncMock(),
    )
    database = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(bootstrap_module, "Database", lambda _: database)
    monkeypatch.setattr(bootstrap_module, "TelegramAdapter", lambda *_: adapter)

    await bootstrap_module.setup_telegram(
        Settings(
            telegram_bot_token="123456:test",  # noqa: S106 - synthetic credential
            telegram_mode="polling",
        )
    )

    bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
    bot.set_webhook.assert_not_awaited()
    assert "telegram_mode=polling\nwebhook=deleted" in capsys.readouterr().out
    adapter.close.assert_awaited_once_with()
    database.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_telegram_setup_webhook_remains_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = SimpleNamespace(
        get_me=AsyncMock(
            return_value=SimpleNamespace(
                username="dca_bot",
                has_topics_enabled=False,
                supports_guest_queries=False,
            )
        ),
        delete_webhook=AsyncMock(return_value=True),
        set_webhook=AsyncMock(return_value=True),
    )
    adapter = SimpleNamespace(
        bot=bot,
        setup_commands=AsyncMock(),
        allowed_updates=lambda: ["message"],
        close=AsyncMock(),
    )
    database = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(bootstrap_module, "Database", lambda _: database)
    monkeypatch.setattr(bootstrap_module, "TelegramAdapter", lambda *_: adapter)
    settings = Settings(
        public_url="https://agency.kakaduai.com",
        telegram_bot_token="123456:test",  # noqa: S106 - synthetic credential
        telegram_mode="webhook",
        telegram_webhook_secret="synthetic-secret",  # noqa: S106
    )

    await bootstrap_module.setup_telegram(settings)

    bot.set_webhook.assert_awaited_once_with(
        url="https://agency.kakaduai.com/webhooks/telegram",
        secret_token="synthetic-secret",  # noqa: S106
        allowed_updates=["message"],
        drop_pending_updates=False,
    )
    bot.delete_webhook.assert_not_awaited()
