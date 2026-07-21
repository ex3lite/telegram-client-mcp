from uuid import UUID

import pytest

from dca.bootstrap import build_parser


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
        ]
    )

    assert args.department == "Mobile"
    assert args.stack == "Android / Kotlin"
    assert args.role is None


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
