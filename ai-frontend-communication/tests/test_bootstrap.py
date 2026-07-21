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
