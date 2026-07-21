from __future__ import annotations

import argparse
import asyncio
import getpass
from uuid import uuid4

import anyio
from argon2 import PasswordHasher
from sqlalchemy import select

from dca.config import Settings, get_settings
from dca.db import (
    Database,
    Project,
    ProjectMembership,
    Repository,
    ServiceAccount,
    ServiceAccountProject,
    TelegramChat,
    TelegramIdentity,
    User,
    append_audit,
)
from dca.mcp import generate_service_token
from dca.telegram import TelegramAdapter

DEFAULT_SERVICE_TOOLS = [
    "identity.resolve_user",
    "telegram.ask_user",
    "telegram.get_clarification",
    "telegram.cancel_clarification",
]


async def seed(args: argparse.Namespace, settings: Settings) -> None:
    database = Database(settings)
    created_token: str | None = None
    try:
        async with database.session() as session:
            project = await session.scalar(select(Project).where(Project.slug == args.project_slug))
            if project is None:
                project = Project(slug=args.project_slug, name=args.project_name)
                session.add(project)
                await session.flush()
            account = await session.scalar(
                select(ServiceAccount).where(ServiceAccount.name == args.service_account_name)
            )
            if account is None:
                token, prefix, token_hash = generate_service_token()
                account = ServiceAccount(
                    name=args.service_account_name,
                    token_prefix=prefix,
                    token_hash=token_hash,
                    tool_scopes=DEFAULT_SERVICE_TOOLS,
                )
                session.add(account)
                await session.flush()
                session.add(
                    ServiceAccountProject(
                        service_account_id=account.id,
                        project_id=project.id,
                    )
                )
                created_token = token
            else:
                association = await session.get(
                    ServiceAccountProject,
                    (account.id, project.id),
                )
                if association is None:
                    session.add(
                        ServiceAccountProject(
                            service_account_id=account.id,
                            project_id=project.id,
                        )
                    )
        print(f"project_id={project.id}")
        print(f"service_account_id={account.id}")
        if created_token:
            print("Store this token now; only its Argon2id hash is persisted:")
            print(created_token)
    finally:
        await database.close()


async def link_user(args: argparse.Namespace, settings: Settings) -> None:
    database = Database(settings)
    try:
        async with database.session() as session:
            project = await session.scalar(select(Project).where(Project.slug == args.project_slug))
            if project is None:
                raise SystemExit(f"Unknown project: {args.project_slug}")
            identity = await session.scalar(
                select(TelegramIdentity).where(
                    TelegramIdentity.telegram_user_id == args.telegram_user_id
                )
            )
            user: User | None
            if identity is None:
                user = User(display_name=args.name, email=args.email)
                session.add(user)
                await session.flush()
                identity = TelegramIdentity(
                    user_id=user.id,
                    telegram_user_id=args.telegram_user_id,
                    username=(args.username or "").removeprefix("@") or None,
                    verified_at=None,
                    reachable=False,
                )
                session.add(identity)
            else:
                user = await session.get(User, identity.user_id)
                if user is None:
                    raise SystemExit("Telegram identity points to a missing user")
                user.display_name = args.name
                if args.email:
                    user.email = args.email
                if args.username:
                    identity.username = args.username.removeprefix("@")
            membership = await session.get(ProjectMembership, (project.id, user.id))
            if membership is None:
                membership = ProjectMembership(
                    project_id=project.id,
                    user_id=user.id,
                    role=args.role or "developer",
                    department=args.department,
                    stack=args.stack,
                )
                session.add(membership)
            else:
                if args.role is not None:
                    membership.role = args.role
                if args.department is not None:
                    membership.department = args.department
                if args.stack is not None:
                    membership.stack = args.stack
            if args.verify:
                from dca.domain import utcnow

                identity.verified_at = utcnow()
            await append_audit(
                session,
                event_type="project.member_profile_upserted",
                correlation_id=f"bootstrap:link-user:{uuid4().hex}",
                actor_type="system",
                actor_id="dca-bootstrap",
                project_id=project.id,
                subject_type="user",
                subject_id=str(user.id),
                payload={
                    "role": membership.role,
                    "department": membership.department,
                    "stack": membership.stack,
                },
            )
        print(f"user_id={user.id}")
        print("The user must open the bot and run /start before the bot can send a DM.")
    finally:
        await database.close()


async def link_chat(args: argparse.Namespace, settings: Settings) -> None:
    database = Database(settings)
    try:
        async with database.session() as session:
            project = await session.scalar(select(Project).where(Project.slug == args.project_slug))
            if project is None:
                raise SystemExit(f"Unknown project: {args.project_slug}")
            chat = await session.scalar(
                select(TelegramChat).where(
                    TelegramChat.telegram_chat_id == args.telegram_chat_id,
                    TelegramChat.message_thread_id == args.message_thread_id,
                )
            )
            if chat is None:
                chat = TelegramChat(
                    project_id=project.id,
                    telegram_chat_id=args.telegram_chat_id,
                    message_thread_id=args.message_thread_id,
                    kind=args.kind,
                    enabled=True,
                )
                session.add(chat)
                await session.flush()
            else:
                chat.project_id = project.id
                chat.kind = args.kind
                chat.enabled = True
        print(f"telegram_chat_id={chat.telegram_chat_id}")
        print(f"project_id={chat.project_id}")
    finally:
        await database.close()


async def add_repository(args: argparse.Namespace, settings: Settings) -> None:
    database = Database(settings)
    try:
        async with database.session() as session:
            project = await session.scalar(select(Project).where(Project.slug == args.project_slug))
            if project is None:
                raise SystemExit(f"Unknown project: {args.project_slug}")
            existing = await session.scalar(
                select(Repository).where(
                    Repository.project_id == project.id,
                    Repository.name == args.name,
                )
            )
            if existing is not None:
                raise SystemExit("Repository with this name already exists in the project")
            paths = [anyio.Path(args.deploy_key_path), anyio.Path(args.known_hosts_path)]
            for candidate in paths:
                if not await candidate.is_file():
                    raise SystemExit(f"File does not exist: {candidate}")
            deploy_key_path, known_hosts_path = [str(await path.resolve()) for path in paths]
            repository = Repository(
                project_id=project.id,
                name=args.name,
                ssh_url=args.ssh_url,
                default_branch=args.branch,
                deploy_key_path=deploy_key_path,
                known_hosts_path=known_hosts_path,
                allowed_paths=args.allowed_path,
            )
            session.add(repository)
            await session.flush()
        print(f"repository_id={repository.id}")
        print(f"Queue synchronization with POST /api/v1/repositories/{repository.id}/sync")
    finally:
        await database.close()


async def setup_telegram(settings: Settings) -> None:
    if not settings.telegram_bot_token.get_secret_value():
        raise SystemExit("DCA_TELEGRAM_BOT_TOKEN is required")
    if not settings.telegram_webhook_secret.get_secret_value():
        raise SystemExit("DCA_TELEGRAM_WEBHOOK_SECRET is required")
    database = Database(settings)
    telegram = TelegramAdapter(settings, database)
    try:
        bot = await telegram.bot.get_me()
        await telegram.setup_commands()
        await telegram.bot.set_webhook(
            url=settings.telegram_webhook_url,
            secret_token=settings.telegram_webhook_secret.get_secret_value(),
            allowed_updates=telegram.allowed_updates(),
            drop_pending_updates=False,
        )
        print(f"bot=@{bot.username}")
        print(f"has_topics_enabled={bool(bot.has_topics_enabled)}")
        print(f"supports_guest_queries={bool(bot.supports_guest_queries)}")
        print(f"webhook={settings.telegram_webhook_url}")
    finally:
        await telegram.close()
        await database.close()


def hash_password() -> None:
    password = getpass.getpass("Admin password: ")
    confirmation = getpass.getpass("Repeat password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match")
    if len(password) < 12:
        raise SystemExit("Use at least 12 characters")
    print(PasswordHasher().hash(password))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dca-bootstrap")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("hash-password")

    seed_parser = subparsers.add_parser("seed")
    seed_parser.add_argument("--project-slug", required=True)
    seed_parser.add_argument("--project-name", required=True)
    seed_parser.add_argument("--service-account-name", default="codex")

    user_parser = subparsers.add_parser("link-user")
    user_parser.add_argument("--project-slug", required=True)
    user_parser.add_argument("--name", required=True)
    user_parser.add_argument("--email")
    user_parser.add_argument("--telegram-user-id", required=True, type=int)
    user_parser.add_argument("--username")
    user_parser.add_argument("--role")
    user_parser.add_argument("--department")
    user_parser.add_argument("--stack")
    user_parser.add_argument("--verify", action="store_true")

    chat_parser = subparsers.add_parser("link-chat")
    chat_parser.add_argument("--project-slug", required=True)
    chat_parser.add_argument("--telegram-chat-id", required=True, type=int)
    chat_parser.add_argument("--message-thread-id", type=int)
    chat_parser.add_argument("--kind", default="group")

    repository_parser = subparsers.add_parser("add-repository")
    repository_parser.add_argument("--project-slug", required=True)
    repository_parser.add_argument("--name", required=True)
    repository_parser.add_argument("--ssh-url", required=True)
    repository_parser.add_argument("--branch", default="main")
    repository_parser.add_argument("--deploy-key-path", required=True)
    repository_parser.add_argument("--known-hosts-path", required=True)
    repository_parser.add_argument("--allowed-path", action="append", default=[])

    subparsers.add_parser("telegram-setup")
    return parser


async def async_main(args: argparse.Namespace, settings: Settings) -> None:
    if args.command == "seed":
        await seed(args, settings)
    elif args.command == "link-user":
        await link_user(args, settings)
    elif args.command == "link-chat":
        await link_chat(args, settings)
    elif args.command == "add-repository":
        await add_repository(args, settings)
    elif args.command == "telegram-setup":
        await setup_telegram(settings)


def run() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "hash-password":
        hash_password()
        return
    asyncio.run(async_main(args, get_settings()))


if __name__ == "__main__":
    run()
