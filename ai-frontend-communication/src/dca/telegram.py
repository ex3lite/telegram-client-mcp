from __future__ import annotations

import html
import re
import secrets
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BufferedInputFile,
    ForceReply,
    InlineQueryResultArticle,
    InputRichBlockThinking,
    InputRichMessage,
    InputRichMessageContent,
    Message,
    ReplyParameters,
    Update,
)
from sqlalchemy import func, select, update

from dca.config import Settings
from dca.db import (
    Clarification,
    Database,
    Interaction,
    Project,
    ProjectMembership,
    Repository,
    TelegramChat,
    TelegramIdentity,
    append_audit,
    enqueue_job,
)
from dca.domain import ChangeRequestCreate, ClarificationStatus, RepositoryStatus, utcnow
from dca.service import (
    ServiceError,
    answer_clarification_from_telegram,
    create_change_request,
    expire_clarification,
)

MAX_RICH_MESSAGE_CHARS = 32_768
MAX_EPHEMERAL_CHARS = 4_096
PROJECT_PREFIX_RE = re.compile(r"^project:([a-z0-9][a-z0-9-]{0,79})\s+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class MessageContext:
    project: Project
    user_id: UUID


class BotMention(Filter):
    async def __call__(self, message: Message, bot: Bot) -> bool | dict[str, Any]:
        if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
            return False
        text = message.text or ""
        if not text.startswith("@"):
            return False
        username = (await bot.me()).username
        if username is None:
            return False
        mention_text = extract_bot_mention(text, username)
        return False if mention_text is None else {"mention_text": mention_text}


class TelegramAdapter:
    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.database = database
        token = settings.telegram_bot_token.get_secret_value() or "123456:development-only"
        proxy = settings.outbound_proxy_url
        session = AiohttpSession(proxy=str(proxy.get_secret_value())) if proxy else None
        self.bot = Bot(token, session=session)
        self.dispatcher = Dispatcher()
        self.router = Router(name="developer-communication-agent")
        self.dispatcher.include_router(self.router)
        self._register_handlers()

    async def close(self) -> None:
        await self.bot.session.close()

    async def setup_commands(self) -> None:
        await self.bot.set_my_commands(
            [
                BotCommand(command="ask", description="Задать вопрос по коду"),
                BotCommand(command="request", description="Создать заявку"),
                BotCommand(command="help", description="Показать формат команд"),
            ],
            scope=BotCommandScopeAllPrivateChats(),
        )
        await self.bot.set_my_commands(
            [
                BotCommand(command="ask", description="Задать вопрос по коду"),
                BotCommand(
                    command="ask_private",
                    description="Задать вопрос приватно",
                    is_ephemeral=True,
                ),
                BotCommand(
                    command="request",
                    description="Создать приватную заявку",
                    is_ephemeral=True,
                ),
            ],
            scope=BotCommandScopeAllGroupChats(),
        )

    def allowed_updates(self) -> list[str]:
        return self.dispatcher.resolve_used_update_types()

    async def process_raw_update(self, payload: dict[str, Any]) -> None:
        update_object = Update.model_validate(payload, context={"bot": self.bot})
        await self.dispatcher.feed_update(self.bot, update_object)

    async def answer_guest_placeholder(self, payload: dict[str, Any]) -> str | None:
        update_object = Update.model_validate(payload, context={"bot": self.bot})
        message = update_object.guest_message
        if message is None or not message.guest_query_id:
            return None
        result = InlineQueryResultArticle(
            id=f"dca-{update_object.update_id}",
            title="Ответ Developer Agent",
            input_message_content=InputRichMessageContent(
                rich_message=InputRichMessage(
                    markdown="**Проверяю проект и источники...**",
                    skip_entity_detection=True,
                )
            ),
        )
        sent = await self.bot.answer_guest_query(
            guest_query_id=message.guest_query_id,
            result=result,
        )
        return sent.inline_message_id

    async def deliver_clarification(self, clarification_id: UUID) -> bool:
        async with self.database.session() as session:
            row = await session.execute(
                select(Clarification, TelegramIdentity, Project)
                .join(
                    TelegramIdentity,
                    TelegramIdentity.user_id == Clarification.recipient_user_id,
                )
                .join(Project, Project.id == Clarification.project_id)
                .where(Clarification.id == clarification_id)
                .with_for_update(of=Clarification)
            )
            result = row.one_or_none()
            if result is None:
                raise ServiceError("request_not_found", "Clarification was not found")
            clarification, identity, project = result
            if clarification.status != ClarificationStatus.PENDING.value:
                return False
            if clarification.expires_at <= utcnow():
                await expire_clarification(session, clarification)
                return False
            if identity.private_chat_id is None or not identity.reachable:
                raise ServiceError("recipient_unreachable", "Recipient cannot receive Telegram DM")
            topic_id = await session.scalar(
                select(TelegramChat.message_thread_id).where(
                    TelegramChat.project_id == clarification.project_id,
                    TelegramChat.telegram_chat_id == identity.private_chat_id,
                    TelegramChat.kind == "private_topic",
                    TelegramChat.enabled.is_(True),
                )
            )
            text = (
                f"Вопрос от AI-агента по проекту {project.name}\n\n"
                f"Контекст: {clarification.context}\n\n"
                f"Вопрос: {clarification.question}\n\n"
                "Ответьте реплаем на это сообщение. Ответ будет передан агенту как "
                "непроверенные данные, а не как команда."
            )
            message = await self.bot.send_message(
                chat_id=identity.private_chat_id,
                message_thread_id=topic_id,
                text=text,
                reply_markup=ForceReply(
                    selective=True,
                    input_field_placeholder="Введите точный ответ для AI-агента",
                ),
            )
            await session.execute(
                update(Clarification)
                .where(Clarification.id == clarification.id)
                .values(
                    telegram_chat_id=message.chat.id,
                    telegram_message_id=message.message_id,
                    updated_at=func.now(),
                )
            )
            await append_audit(
                session,
                event_type="clarification.delivery_accepted",
                correlation_id=clarification.correlation_id,
                actor_type="system",
                actor_id="telegram-worker",
                project_id=clarification.project_id,
                subject_type="clarification",
                subject_id=str(clarification.id),
                payload={"telegram_message_id": message.message_id},
            )
            return True

    async def notify_clarification_cancelled(self, clarification_id: UUID) -> None:
        async with self.database.session() as session:
            clarification = await session.get(Clarification, clarification_id)
            if (
                clarification is None
                or clarification.telegram_chat_id is None
                or clarification.telegram_message_id is None
            ):
                return
            await self.bot.send_message(
                chat_id=clarification.telegram_chat_id,
                text="Этот вопрос отменён инициировавшим AI-агентом.",
                reply_parameters=ReplyParameters(message_id=clarification.telegram_message_id),
            )

    async def send_knowledge_progress(self, interaction: Interaction, draft_id: int) -> None:
        delivery = interaction.source_ref.get("delivery", {})
        kind = delivery.get("kind")
        if kind != "private_draft":
            return
        kwargs = {
            "chat_id": int(delivery["chat_id"]),
            "message_thread_id": delivery.get("message_thread_id"),
            "draft_id": draft_id,
        }
        try:
            await self.bot.send_rich_message_draft(
                **kwargs,
                rich_message=InputRichMessage(
                    blocks=[InputRichBlockThinking(text="Проверяю код и подтверждаю ссылки")],
                    skip_entity_detection=True,
                ),
            )
        except TelegramBadRequest:
            await self.bot.send_rich_message_draft(
                **kwargs,
                rich_message=plain_rich_message("Проверяю код и подтверждаю ссылки"),
            )

    async def publish_knowledge_answer(
        self,
        interaction: Interaction,
        answer_markdown: str,
    ) -> None:
        delivery = interaction.source_ref.get("delivery", {})
        kind = delivery.get("kind")
        short_answer, attachment = split_rich_answer(answer_markdown)
        rich = InputRichMessage(markdown=short_answer, skip_entity_detection=True)
        if kind == "private_draft":
            kwargs = {
                "chat_id": int(delivery["chat_id"]),
                "message_thread_id": delivery.get("message_thread_id"),
            }
            try:
                sent = await self.bot.send_rich_message(**kwargs, rich_message=rich)
            except TelegramBadRequest:
                sent = await self.bot.send_rich_message(
                    **kwargs,
                    rich_message=plain_rich_message(short_answer),
                )
            if attachment is not None:
                await self.bot.send_document(
                    chat_id=sent.chat.id,
                    message_thread_id=delivery.get("message_thread_id"),
                    document=BufferedInputFile(attachment.encode(), filename="answer.md"),
                    caption="Полный ответ с источниками",
                )
        elif kind == "guest":
            kwargs = {"inline_message_id": str(delivery["inline_message_id"])}
            try:
                await self.bot.edit_message_text(**kwargs, rich_message=rich)
            except TelegramBadRequest:
                await self.bot.edit_message_text(
                    **kwargs,
                    rich_message=plain_rich_message(short_answer),
                )
        elif kind == "ephemeral":
            await self.bot.edit_ephemeral_message_text(
                chat_id=int(delivery["chat_id"]),
                receiver_user_id=int(delivery["receiver_user_id"]),
                ephemeral_message_id=int(delivery["ephemeral_message_id"]),
                text=short_answer[:MAX_EPHEMERAL_CHARS],
            )
        else:
            kwargs = {
                "chat_id": int(delivery["chat_id"]),
                "message_id": int(delivery["message_id"]),
            }
            try:
                await self.bot.edit_message_text(**kwargs, rich_message=rich)
            except TelegramBadRequest:
                await self.bot.edit_message_text(
                    **kwargs,
                    rich_message=plain_rich_message(short_answer),
                )
            if attachment is not None:
                await self.bot.send_document(
                    chat_id=int(delivery["chat_id"]),
                    message_thread_id=delivery.get("message_thread_id"),
                    document=BufferedInputFile(attachment.encode(), filename="answer.md"),
                    caption="Полный ответ с источниками",
                )

    async def publish_knowledge_error(self, interaction: Interaction) -> None:
        delivery = interaction.source_ref.get("delivery", {})
        kind = delivery.get("kind")
        safe = (
            "Не удалось подготовить подтверждённый ответ. "
            f"Повторите запрос позже или сообщите администратору ID {str(interaction.id)[:8]}."
        )
        if kind == "guest":
            await self.bot.edit_message_text(
                inline_message_id=str(delivery["inline_message_id"]), text=safe
            )
        elif kind == "ephemeral":
            await self.bot.edit_ephemeral_message_text(
                chat_id=int(delivery["chat_id"]),
                receiver_user_id=int(delivery["receiver_user_id"]),
                ephemeral_message_id=int(delivery["ephemeral_message_id"]),
                text=safe,
            )
        elif kind == "group_message":
            await self.bot.edit_message_text(
                chat_id=int(delivery["chat_id"]),
                message_id=int(delivery["message_id"]),
                text=safe,
            )
        else:
            await self.bot.send_message(
                chat_id=int(delivery["chat_id"]),
                message_thread_id=delivery.get("message_thread_id"),
                text=safe,
            )

    def _register_handlers(self) -> None:
        @self.router.message(Command("start"))
        async def start(message: Message) -> None:
            if message.from_user is None or message.chat.type != ChatType.PRIVATE:
                return
            async with self.database.session() as session:
                identity = await session.scalar(
                    select(TelegramIdentity).where(
                        TelegramIdentity.telegram_user_id == message.from_user.id
                    )
                )
                if identity is None:
                    await message.answer(
                        "Telegram-аккаунт ещё не связан. Передайте администратору ваш Telegram ID: "
                        f"{message.from_user.id}"
                    )
                    return
                identity.private_chat_id = message.chat.id
                identity.reachable = True
                identity.username = message.from_user.username
                await message.answer(
                    "Связь подтверждена. Теперь вы можете получать адресные вопросы "
                    "по своим проектам."
                )

        @self.router.message(Command("help"))
        async def help_command(message: Message) -> None:
            await self._reply(
                message,
                "Команды:\n"
                "/ask [project:slug] вопрос\n"
                "/ask_private [project:slug] вопрос\n"
                "/request [project:slug] bug|task|feature Заголовок | Подробности",
            )

        @self.router.message(Command("request"))
        async def request_command(
            message: Message,
            command: CommandObject,
            event_update: Update,
        ) -> None:
            if not await self._ephemeral_available(message):
                return
            args = (command.args or "").strip()
            explicit_project, args = extract_project_prefix(args)
            parts = args.split(maxsplit=1)
            if len(parts) != 2 or parts[0] not in {"bug", "task", "feature"}:
                await self._reply(
                    message,
                    "Формат: /request [project:slug] bug|task|feature Заголовок | Подробности",
                    prefer_ephemeral=True,
                )
                return
            title, separator, description = parts[1].partition("|")
            try:
                async with self.database.session() as session:
                    context = await resolve_context(
                        session,
                        message=message,
                        explicit_project_slug=explicit_project,
                    )
                    correlation_id = f"tg:{event_update.update_id}:{uuid4().hex[:12]}"
                    change_request = await create_change_request(
                        session,
                        request=ChangeRequestCreate(
                            project_id=context.project.id,
                            kind=parts[0],
                            title=title.strip(),
                            description=description.strip() if separator else "",
                        ),
                        correlation_id=correlation_id,
                        source="telegram",
                        source_ref={
                            "chat_id": message.chat.id,
                            "message_id": message.message_id,
                            "ephemeral_message_id": message.ephemeral_message_id,
                        },
                        created_by_user_id=context.user_id,
                    )
                await self._reply(
                    message,
                    f"Заявка {str(change_request.id)[:8]} создана и видна администратору.",
                    prefer_ephemeral=True,
                )
            except (ServiceError, ValueError) as exc:
                await self._reply(message, str(exc), prefer_ephemeral=True)

        @self.router.message(Command("ask", "ask_private"))
        async def ask_command(
            message: Message,
            command: CommandObject,
            event_update: Update,
        ) -> None:
            prefer_ephemeral = command.command == "ask_private"
            if prefer_ephemeral and not await self._ephemeral_available(message):
                return
            args = (command.args or "").strip()
            explicit_project, question = extract_project_prefix(args)
            if not question:
                await self._reply(
                    message,
                    "Формат: /ask [project:slug] точный вопрос по коду",
                    prefer_ephemeral=prefer_ephemeral,
                )
                return
            await self._queue_code_question(
                message=message,
                event_update=event_update,
                question=question,
                explicit_project_slug=explicit_project,
                prefer_ephemeral=prefer_ephemeral,
            )

        @self.router.guest_message(F.text)
        async def guest_question(message: Message, event_update: Update) -> None:
            extra = event_update.model_extra or {}
            inline_message_id = extra.get("_dca_inline_message_id")
            caller = message.guest_bot_caller_user
            if inline_message_id is None or caller is None:
                return
            explicit_project, question = extract_project_prefix(message.text or "")
            try:
                async with self.database.session() as session:
                    context = await resolve_context(
                        session,
                        message=message,
                        explicit_project_slug=explicit_project,
                        override_telegram_user_id=caller.id,
                    )
                    await queue_interaction(
                        session,
                        context=context,
                        question=question,
                        correlation_id=f"guest:{message.guest_query_id}",
                        source_ref={
                            "guest_query_id": message.guest_query_id,
                            "delivery": {
                                "kind": "guest",
                                "inline_message_id": inline_message_id,
                            },
                        },
                    )
            except (ServiceError, ValueError) as exc:
                await self.bot.edit_message_text(
                    inline_message_id=str(inline_message_id),
                    text=f"Не удалось определить доступный проект: {exc}",
                )

        @self.router.message(BotMention())
        async def mention_question(
            message: Message,
            event_update: Update,
            mention_text: str,
        ) -> None:
            explicit_project, question = extract_project_prefix(mention_text)
            if not question:
                await self._reply(
                    message,
                    "Формат: @bot [project:slug] точный вопрос по коду",
                )
                return
            await self._queue_code_question(
                message=message,
                event_update=event_update,
                question=question,
                explicit_project_slug=explicit_project,
                prefer_ephemeral=False,
            )

        @self.router.message(F.reply_to_message, F.text)
        async def clarification_reply(message: Message) -> None:
            if message.from_user is None or message.reply_to_message is None:
                return
            try:
                async with self.database.session() as session:
                    await answer_clarification_from_telegram(
                        session,
                        telegram_user_id=message.from_user.id,
                        telegram_chat_id=message.chat.id,
                        reply_to_message_id=message.reply_to_message.message_id,
                        answer=message.text or "",
                    )
                await message.answer("Ответ сохранён и доступен инициировавшему AI-агенту.")
            except ServiceError as exc:
                if exc.code != "request_not_found":
                    await message.answer(exc.message)

    async def _queue_code_question(
        self,
        *,
        message: Message,
        event_update: Update,
        question: str,
        explicit_project_slug: str | None,
        prefer_ephemeral: bool,
    ) -> None:
        if prefer_ephemeral and not await self._ephemeral_available(message):
            return
        try:
            async with self.database.session() as session:
                context = await resolve_context(
                    session,
                    message=message,
                    explicit_project_slug=explicit_project_slug,
                )
                delivery: dict[str, Any]
                if (
                    prefer_ephemeral
                    and message.from_user is not None
                    and message.ephemeral_message_id is not None
                ):
                    placeholder = await self.bot.send_message(
                        chat_id=message.chat.id,
                        receiver_user_id=message.from_user.id,
                        reply_parameters=ReplyParameters(
                            ephemeral_message_id=message.ephemeral_message_id
                        ),
                        text="Проверяю код и источники...",
                    )
                    if placeholder.ephemeral_message_id is None:
                        raise ServiceError(
                            "telegram_invalid_response",
                            "Telegram did not return an ephemeral message ID",
                        )
                    delivery = {
                        "kind": "ephemeral",
                        "chat_id": message.chat.id,
                        "receiver_user_id": message.from_user.id,
                        "ephemeral_message_id": placeholder.ephemeral_message_id,
                    }
                elif message.chat.type == ChatType.PRIVATE:
                    delivery = {
                        "kind": "private_draft",
                        "chat_id": message.chat.id,
                        "message_thread_id": message.message_thread_id,
                    }
                else:
                    placeholder = await message.answer("Проверяю код и подтверждаю источники...")
                    delivery = {
                        "kind": "group_message",
                        "chat_id": placeholder.chat.id,
                        "message_id": placeholder.message_id,
                        "message_thread_id": message.message_thread_id,
                    }
                await queue_interaction(
                    session,
                    context=context,
                    question=question,
                    correlation_id=f"tg:{event_update.update_id}:{uuid4().hex[:12]}",
                    source_ref={
                        "update_id": event_update.update_id,
                        "telegram_user_id": message.from_user.id if message.from_user else None,
                        "delivery": delivery,
                    },
                )
        except (ServiceError, ValueError) as exc:
            await self._reply(message, str(exc), prefer_ephemeral=prefer_ephemeral)

    async def _reply(
        self,
        message: Message,
        text: str,
        *,
        prefer_ephemeral: bool = False,
    ) -> Message | None:
        if prefer_ephemeral and not await self._ephemeral_available(message):
            return None
        if (
            prefer_ephemeral
            and message.from_user is not None
            and message.ephemeral_message_id is not None
        ):
            return await self.bot.send_message(
                chat_id=message.chat.id,
                receiver_user_id=message.from_user.id,
                reply_parameters=ReplyParameters(ephemeral_message_id=message.ephemeral_message_id),
                text=text[:MAX_EPHEMERAL_CHARS],
            )
        return await message.answer(text)

    async def _ephemeral_available(self, message: Message) -> bool:
        if message.chat.type == ChatType.PRIVATE or message.ephemeral_message_id is not None:
            return True
        if message.from_user is not None:
            try:
                await self.bot.send_message(
                    chat_id=message.from_user.id,
                    text=(
                        "Приватная команда недоступна в этом чате. Откройте диалог с ботом, "
                        "нажмите /start и повторите запрос там."
                    ),
                )
            except (TelegramBadRequest, TelegramForbiddenError):
                pass
        return False


def extract_project_prefix(value: str) -> tuple[str | None, str]:
    match = PROJECT_PREFIX_RE.match(value)
    if match is None:
        return None, value.strip()
    return match.group(1).casefold(), value[match.end() :].strip()


def extract_bot_mention(value: str, username: str) -> str | None:
    mention = f"@{username}"
    if not value.casefold().startswith(mention.casefold()):
        return None
    remainder = value[len(mention) :]
    if remainder and not remainder[0].isspace():
        return None
    return remainder.strip()


async def resolve_context(
    session: Any,
    *,
    message: Message,
    explicit_project_slug: str | None,
    override_telegram_user_id: int | None = None,
) -> MessageContext:
    telegram_user_id = override_telegram_user_id or (
        message.from_user.id if message.from_user else None
    )
    if telegram_user_id is None:
        raise ServiceError("identity_not_found", "Telegram user is unavailable")
    identity = await session.scalar(
        select(TelegramIdentity).where(
            TelegramIdentity.telegram_user_id == telegram_user_id,
            TelegramIdentity.verified_at.is_not(None),
        )
    )
    if identity is None:
        raise ServiceError("consent_required", "Telegram identity is not linked and verified")

    if explicit_project_slug is not None:
        project = await session.scalar(
            select(Project)
            .join(ProjectMembership, ProjectMembership.project_id == Project.id)
            .where(
                Project.slug == explicit_project_slug,
                ProjectMembership.user_id == identity.user_id,
                Project.enabled.is_(True),
            )
        )
        if project is None:
            raise ServiceError("project_scope_violation", "Project is unavailable to this user")
        return MessageContext(project=project, user_id=identity.user_id)

    project = await session.scalar(
        select(Project)
        .join(TelegramChat, TelegramChat.project_id == Project.id)
        .join(
            ProjectMembership,
            (ProjectMembership.project_id == Project.id)
            & (ProjectMembership.user_id == identity.user_id),
        )
        .where(
            TelegramChat.telegram_chat_id == message.chat.id,
            TelegramChat.enabled.is_(True),
            Project.enabled.is_(True),
            (
                (TelegramChat.message_thread_id == message.message_thread_id)
                | (TelegramChat.message_thread_id.is_(None))
            ),
        )
        .order_by(TelegramChat.message_thread_id.desc().nullslast())
    )
    if project is not None:
        return MessageContext(project=project, user_id=identity.user_id)

    projects = list(
        await session.scalars(
            select(Project)
            .join(ProjectMembership, ProjectMembership.project_id == Project.id)
            .where(
                ProjectMembership.user_id == identity.user_id,
                Project.enabled.is_(True),
            )
            .limit(2)
        )
    )
    if len(projects) == 1:
        return MessageContext(project=projects[0], user_id=identity.user_id)
    raise ServiceError(
        "project_required",
        "Укажите проект в начале команды: project:slug",
    )


async def queue_interaction(
    session: Any,
    *,
    context: MessageContext,
    question: str,
    correlation_id: str,
    source_ref: dict[str, Any],
) -> Interaction:
    repository = await session.scalar(
        select(Repository)
        .where(
            Repository.project_id == context.project.id,
            Repository.status == RepositoryStatus.READY.value,
            Repository.current_commit.is_not(None),
        )
        .order_by(Repository.created_at)
    )
    if repository is None or repository.current_commit is None:
        raise ServiceError("source_unavailable", "У проекта нет готового Git snapshot")
    interaction = Interaction(
        project_id=context.project.id,
        repository_id=repository.id,
        correlation_id=correlation_id,
        source="telegram",
        source_ref=source_ref,
        question=question[:8_000],
        commit_sha=repository.current_commit,
    )
    session.add(interaction)
    await session.flush()
    await enqueue_job(
        session,
        kind="knowledge.answer",
        payload={"interaction_id": str(interaction.id)},
        deduplication_key=f"interaction:{interaction.id}:answer",
        max_attempts=3,
    )
    await append_audit(
        session,
        event_type="knowledge.answer_requested",
        correlation_id=correlation_id,
        actor_type="user",
        actor_id=str(context.user_id),
        project_id=context.project.id,
        subject_type="interaction",
        subject_id=str(interaction.id),
        payload={"repository_id": str(repository.id), "commit": repository.current_commit},
    )
    return interaction


def split_rich_answer(answer: str) -> tuple[str, str | None]:
    if len(answer) <= MAX_RICH_MESSAGE_CHARS:
        return answer, None
    preview = answer[: MAX_RICH_MESSAGE_CHARS - 220].rstrip()
    preview += "\n\nПолный ответ превышает лимит Rich Message и приложен файлом `answer.md`."
    return preview, answer


def plain_rich_message(text: str) -> InputRichMessage:
    return InputRichMessage(html=html.escape(text), skip_entity_detection=True)


def new_draft_id() -> int:
    return secrets.randbelow(2_147_483_646) + 1
