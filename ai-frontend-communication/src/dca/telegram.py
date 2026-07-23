from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID, uuid4

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatAction, ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import JOIN_TRANSITION, ChatMemberUpdatedFilter, Command, CommandObject, Filter
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BufferedInputFile,
    ChatMemberUpdated,
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
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

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
    TelegramUpdate,
    User,
    append_audit,
    enqueue_job,
)
from dca.domain import (
    ChangeRequestCreate,
    ClarificationStatus,
    KnowledgeArtifact,
    RepositoryStatus,
    utcnow,
)
from dca.memory import append_conversation_message, get_or_create_conversation_thread
from dca.privacy import (
    SECURITY_GUARD_ROLE,
    guard_request_kinds,
    sanitize_text,
)
from dca.service import (
    ServiceError,
    answer_clarification_from_telegram,
    create_change_request,
    expire_clarification,
    is_safe_markdown_attachment_name,
    load_project_agent_settings,
    project_member_profile,
)

MAX_RICH_MESSAGE_CHARS = 32_768
MAX_EPHEMERAL_CHARS = 4_096
MAX_THINKING_DRAFT_CHARS = 8_000
MAX_INTERACTION_QUESTION_CHARS = 32_000
KNOWLEDGE_PROGRESS_TEXT = "Разбираюсь и собираю ответ"
GROUP_WELCOME_TEXT = (
    "Привет! Меня зовут Братулец. Можешь писать мне тут или в личку.\n\n"
    "Пиши, если надо что-то узнать про бэкенд или передать бэкенд-разработчикам "
    "заявку на дополнение."
)
GROUP_WELCOME_UNBOUND_SUFFIX = (
    "\n\nЧтобы я отвечал в этой группе, администратору осталось добавить чат в whitelist панели."
)
PROJECT_PREFIX_RE = re.compile(r"^project:([a-z0-9][a-z0-9-]{0,79})\s+", re.IGNORECASE)
BOT_NAME_ALIASES = (
    "братулец агент",
    "братулец",
    "агентик агент",
    "агентик",
    "kakadu ai agent",
    "kakadu ai",
)
DOCUMENT_ACTION_RE = re.compile(
    r"\b(?:создай|сделай|подготовь|напиши|сгенерируй|оформи|выгрузи|"
    r"дай|скинь|пришли|отправь|прикрепи|create|write|generate|prepare|export|"
    r"give|send|attach)\b",
    re.IGNORECASE,
)
DOCUMENT_SUBJECT_RE = re.compile(
    r"(?:\b(?:документац\w*|инструкц\w*|спецификац\w*|гайд\w*|отч[её]т\w*|"
    r"файл\w*|readme(?:\.md)?|runbook|markdown|md[- ]?файл\w*|file|"
    r"documentation|guide|specification|report)\b|\b[\w.-]+\.md\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class MessageContext:
    project: Project
    user_id: UUID
    chat_id: UUID | None
    requester_profile: dict[str, Any]
    telegram_user_id: int


class BotMention(Filter):
    async def __call__(self, message: Message, bot: Bot) -> bool | dict[str, Any]:
        if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
            return False
        text = message.text or ""
        bot_user = await bot.me()
        mention_text = extract_bot_call(
            text,
            username=bot_user.username,
            first_name=bot_user.first_name,
        )
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
        await self.bot.set_my_name(name="Братулец")
        await self.bot.set_my_commands(
            [
                BotCommand(command="ask", description="Задать вопрос Братульцу"),
                BotCommand(command="request", description="Создать заявку"),
                BotCommand(command="help", description="Показать формат команд"),
            ],
            scope=BotCommandScopeAllPrivateChats(),
        )
        await self.bot.set_my_commands(
            [
                BotCommand(command="ask", description="Задать вопрос Братульцу"),
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
            title="Ответ Братульца",
            input_message_content=InputRichMessageContent(
                rich_message=InputRichMessage(
                    markdown="**Думаю...**",
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

    async def deliver_agent_message(
        self,
        *,
        chat_id: int,
        message_thread_id: int | None,
        text_markdown: str,
        attachment_name: str | None,
        attachment_markdown: str | None,
    ) -> int:
        if (attachment_name is None) != (attachment_markdown is None):
            raise ServiceError("invalid_attachment", "Agent message attachment is incomplete")
        if attachment_name is None:
            try:
                sent = await self.bot.send_rich_message(
                    chat_id=chat_id,
                    message_thread_id=message_thread_id,
                    rich_message=InputRichMessage(
                        markdown=text_markdown,
                        skip_entity_detection=True,
                    ),
                )
            except TelegramBadRequest:
                sent = await self.bot.send_message(
                    chat_id=chat_id,
                    message_thread_id=message_thread_id,
                    text=text_markdown,
                )
            return sent.message_id
        assert attachment_markdown is not None
        if len(text_markdown) > 1_024:
            raise ServiceError(
                "invalid_message", "Agent message caption must not exceed 1024 characters"
            )
        if not is_safe_markdown_attachment_name(attachment_name):
            raise ServiceError("invalid_attachment", "Agent message attachment name is unsafe")
        sent = await self.bot.send_document(
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            document=BufferedInputFile(attachment_markdown.encode(), filename=attachment_name),
            caption=text_markdown,
        )
        return sent.message_id

    async def send_knowledge_progress(self, interaction: Interaction) -> None:
        delivery = interaction.source_ref.get("delivery", {})
        kind = delivery.get("kind")
        if kind == "group_message":
            await self.bot.send_chat_action(
                chat_id=int(delivery["chat_id"]),
                message_thread_id=delivery.get("message_thread_id"),
                action=ChatAction.TYPING,
            )
            return
        if kind != "private_draft":
            return
        kwargs = {
            "chat_id": int(delivery["chat_id"]),
            "message_thread_id": delivery.get("message_thread_id"),
            "draft_id": draft_id_for_interaction(interaction.id),
        }
        try:
            await self.bot.send_rich_message_draft(
                **kwargs,
                rich_message=InputRichMessage(
                    blocks=[InputRichBlockThinking(text=KNOWLEDGE_PROGRESS_TEXT)],
                    skip_entity_detection=True,
                ),
            )
        except TelegramBadRequest:
            await self.bot.send_message_draft(**kwargs, text="")

    async def send_knowledge_stream(
        self,
        interaction: Interaction,
        *,
        answer_markdown: str,
        thinking: str,
    ) -> dict[str, Any] | None:
        delivery = interaction.source_ref.get("delivery", {})
        kind = delivery.get("kind")
        if kind in {"group_message", "private_message"}:
            thinking_preview = thinking[-MAX_THINKING_DRAFT_CHARS:]
            if answer_markdown:
                preview = answer_markdown[:MAX_RICH_MESSAGE_CHARS]
            elif kind == "group_message" and thinking_preview:
                preview = f"💭 Mind\n{thinking_preview}"[:MAX_RICH_MESSAGE_CHARS]
            else:
                preview = ""
            if preview:
                try:
                    await self.bot.edit_message_text(
                        chat_id=int(delivery["chat_id"]),
                        message_id=int(delivery["message_id"]),
                        rich_message=knowledge_rich_message(interaction, preview),
                    )
                except TelegramBadRequest:
                    await self.bot.edit_message_text(
                        chat_id=int(delivery["chat_id"]),
                        message_id=int(delivery["message_id"]),
                        rich_message=plain_rich_message(preview),
                    )
            if kind == "group_message":
                await self.bot.send_chat_action(
                    chat_id=int(delivery["chat_id"]),
                    message_thread_id=delivery.get("message_thread_id"),
                    action=ChatAction.TYPING,
                )
            return dict(delivery)
        if kind != "private_draft":
            return None
        kwargs = {
            "chat_id": int(delivery["chat_id"]),
            "message_thread_id": delivery.get("message_thread_id"),
            "draft_id": draft_id_for_interaction(interaction.id),
        }
        thinking_preview = thinking[-MAX_THINKING_DRAFT_CHARS:]
        if not answer_markdown:
            rich_message = InputRichMessage(
                blocks=[InputRichBlockThinking(text=thinking_preview or KNOWLEDGE_PROGRESS_TEXT)],
                skip_entity_detection=True,
            )
            try:
                await self.bot.send_rich_message_draft(**kwargs, rich_message=rich_message)
            except TelegramBadRequest:
                await self.bot.send_message_draft(
                    **kwargs,
                    text=(thinking_preview or KNOWLEDGE_PROGRESS_TEXT)[:MAX_EPHEMERAL_CHARS],
                )
            return None

        persistent_kwargs = {
            "chat_id": int(delivery["chat_id"]),
            "message_thread_id": delivery.get("message_thread_id"),
        }
        preview = answer_markdown[:MAX_RICH_MESSAGE_CHARS]
        try:
            sent = await self.bot.send_rich_message(
                **persistent_kwargs,
                rich_message=knowledge_rich_message(interaction, preview),
            )
        except TelegramBadRequest:
            sent = await self.bot.send_rich_message(
                **persistent_kwargs,
                rich_message=plain_rich_message(preview),
            )
        return {
            "kind": "private_message",
            "chat_id": sent.chat.id,
            "message_id": sent.message_id,
            "message_thread_id": delivery.get("message_thread_id"),
        }

    async def publish_knowledge_answer(
        self,
        interaction: Interaction,
        answer_markdown: str,
        *,
        artifacts: list[dict[str, Any]] | None = None,
        attach_markdown: bool = True,
    ) -> None:
        delivery = interaction.source_ref.get("delivery", {})
        kind = delivery.get("kind")
        short_answer, attachment = split_rich_answer(
            answer_markdown,
            attach_markdown=attach_markdown,
        )
        chunks = (
            rich_answer_chunks(answer_markdown)
            if not attach_markdown and kind in {"private_draft", "private_message", "group_message"}
            else [short_answer]
        )
        documents = markdown_documents(artifacts or [], attachment=attachment)
        if kind == "private_draft":
            kwargs = {
                "chat_id": int(delivery["chat_id"]),
                "message_thread_id": delivery.get("message_thread_id"),
            }
            sent_chat_id = int(delivery["chat_id"])
            for chunk in chunks:
                try:
                    sent = await self.bot.send_rich_message(
                        **kwargs,
                        rich_message=knowledge_rich_message(interaction, chunk),
                    )
                except TelegramBadRequest:
                    sent = await self.bot.send_rich_message(
                        **kwargs,
                        rich_message=plain_rich_message(chunk),
                    )
                sent_chat_id = sent.chat.id
            if attach_markdown:
                await self._send_markdown_documents(
                    chat_id=sent_chat_id,
                    message_thread_id=delivery.get("message_thread_id"),
                    documents=documents,
                )
        elif kind == "guest":
            kwargs = {"inline_message_id": str(delivery["inline_message_id"])}
            try:
                await self.bot.edit_message_text(
                    **kwargs,
                    rich_message=knowledge_rich_message(interaction, short_answer),
                )
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
            first_chunk, *remaining_chunks = chunks
            try:
                await self.bot.edit_message_text(
                    **kwargs,
                    rich_message=knowledge_rich_message(interaction, first_chunk),
                )
            except TelegramBadRequest:
                await self.bot.edit_message_text(
                    **kwargs,
                    rich_message=plain_rich_message(first_chunk),
                )
            for chunk in remaining_chunks:
                send_kwargs = {
                    "chat_id": int(delivery["chat_id"]),
                    "message_thread_id": delivery.get("message_thread_id"),
                }
                try:
                    await self.bot.send_rich_message(
                        **send_kwargs,
                        rich_message=knowledge_rich_message(interaction, chunk),
                    )
                except TelegramBadRequest:
                    await self.bot.send_rich_message(
                        **send_kwargs,
                        rich_message=plain_rich_message(chunk),
                    )
            if attach_markdown:
                await self._send_markdown_documents(
                    chat_id=int(delivery["chat_id"]),
                    message_thread_id=delivery.get("message_thread_id"),
                    documents=documents,
                )

    async def _send_markdown_documents(
        self,
        *,
        chat_id: int,
        message_thread_id: int | None,
        documents: list[KnowledgeArtifact],
    ) -> None:
        for artifact in documents:
            await self.bot.send_document(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                document=BufferedInputFile(artifact.content.encode(), filename=artifact.name),
                caption=f"Документ: {artifact.name}",
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
        elif kind in {"group_message", "private_message"}:
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
        @self.router.my_chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
        async def welcome_group(event: ChatMemberUpdated) -> None:
            if event.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
                return
            async with self.database.session() as session:
                bound_chat_id = await session.scalar(
                    select(TelegramChat.id)
                    .join(Project, Project.id == TelegramChat.project_id)
                    .where(
                        TelegramChat.telegram_chat_id == event.chat.id,
                        TelegramChat.enabled.is_(True),
                        Project.enabled.is_(True),
                    )
                    .limit(1)
                )
            await self.bot.send_message(
                chat_id=event.chat.id,
                text=(
                    GROUP_WELCOME_TEXT
                    if bound_chat_id is not None
                    else GROUP_WELCOME_TEXT + GROUP_WELCOME_UNBOUND_SUFFIX
                ),
            )

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
                identity.verified_at = utcnow()
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
                "/ask [project:slug] вопрос Братульцу\n"
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
                    "Формат: /ask [project:slug] вопрос",
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
                    "Формат: Братулец, [project:slug] вопрос",
                )
                return
            await self._queue_code_question(
                message=message,
                event_update=event_update,
                question=question,
                explicit_project_slug=explicit_project,
                prefer_ephemeral=False,
                allowed_modes={"mentions", "all_messages"},
            )

        @self.router.message(F.reply_to_message, F.text)
        async def clarification_reply(message: Message) -> None:
            if message.from_user is None or message.reply_to_message is None:
                return
            try:
                async with self.database.session() as session:
                    clarification = await answer_clarification_from_telegram(
                        session,
                        telegram_user_id=message.from_user.id,
                        telegram_chat_id=message.chat.id,
                        reply_to_message_id=message.reply_to_message.message_id,
                        answer=message.text or "",
                    )
                    agent_settings = await load_project_agent_settings(
                        session,
                        clarification.project_id,
                    )
                    if agent_settings.memory_enabled:
                        thread = await get_or_create_conversation_thread(
                            session,
                            project_id=clarification.project_id,
                            chat_id=None,
                            user_id=clarification.recipient_user_id,
                        )
                        await append_conversation_message(
                            session,
                            project_id=clarification.project_id,
                            chat_id=None,
                            user_id=clarification.recipient_user_id,
                            thread_id=thread.id,
                            role="user",
                            source="telegram",
                            content=clarification.answer_raw or "",
                            external_id=f"clarification:{clarification.id}:answer",
                            author_user_id=clarification.recipient_user_id,
                        )
                await message.answer("Ответ сохранён и доступен инициировавшему AI-агенту.")
            except ServiceError as exc:
                if exc.code == "privacy_blocked" and isinstance(
                    exc.metadata.get("project_id"), str
                ):
                    async with self.database.session() as session:
                        await append_audit(
                            session,
                            event_type="clarification.answer_privacy_blocked",
                            correlation_id=str(exc.metadata.get("correlation_id") or "unknown"),
                            actor_type="user",
                            actor_id=str(message.from_user.id),
                            project_id=UUID(str(exc.metadata["project_id"])),
                            subject_type="clarification",
                            subject_id=str(exc.metadata.get("clarification_id") or "unknown"),
                            outcome="blocked",
                            payload={
                                "privacy_findings_count": exc.metadata.get(
                                    "privacy_findings_count", 0
                                ),
                                "privacy_findings": exc.metadata.get("privacy_findings", []),
                            },
                        )
                if exc.code != "request_not_found":
                    await message.answer(exc.message)

        @self.router.message(F.text)
        async def configured_text_question(message: Message, event_update: Update) -> None:
            text = (message.text or "").strip()
            if (
                not text
                or text.startswith("/")
                or message.reply_to_message is not None
                or (message.from_user is not None and message.from_user.is_bot)
            ):
                return
            explicit_project, question = extract_project_prefix(text)
            await self._queue_code_question(
                message=message,
                event_update=event_update,
                question=question,
                explicit_project_slug=explicit_project,
                prefer_ephemeral=False,
                allowed_modes={"all_messages"},
            )

    async def _queue_code_question(
        self,
        *,
        message: Message,
        event_update: Update,
        question: str,
        explicit_project_slug: str | None,
        prefer_ephemeral: bool,
        allowed_modes: set[str] | None = None,
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
                agent_settings = await load_project_agent_settings(session, context.project.id)
                if not agent_settings.enabled:
                    raise ServiceError("agent_disabled", "Агент отключён для этого проекта")
                mode = (
                    agent_settings.telegram_private_mode
                    if message.chat.type == ChatType.PRIVATE
                    else agent_settings.telegram_group_mode
                )
                if allowed_modes is not None and mode not in allowed_modes:
                    return
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
                        text="Думаю...",
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
                    placeholder = await message.answer("💭 Думаю...")
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
            if (
                isinstance(exc, ServiceError)
                and exc.code == "chat_unavailable"
                and allowed_modes == {"all_messages"}
            ):
                return
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


async def ingest_telegram_update(
    session: AsyncSession,
    telegram: TelegramAdapter,
    payload: dict[str, Any],
    *,
    actor_id: str,
) -> bool:
    """Durably reserve and enqueue one update from either Telegram transport."""
    update_id = int(payload["update_id"])
    if not await reserve_telegram_update(session, update_id, payload):
        return False
    if payload.get("guest_message") is not None:
        try:
            inline_message_id = await telegram.answer_guest_placeholder(payload)
        except Exception as exc:
            await mark_guest_uncertain(session, update_id, type(exc).__name__, actor_id=actor_id)
            return True
        if inline_message_id is not None:
            payload["_dca_inline_message_id"] = inline_message_id
    await queue_telegram_update(session, update_id, payload)
    return True


async def reserve_telegram_update(
    session: AsyncSession,
    update_id: int,
    payload: dict[str, Any],
) -> bool:
    update_type = next((key for key in payload if key != "update_id"), "unknown")
    result = await session.execute(
        insert(TelegramUpdate)
        .values(update_id=update_id, update_type=update_type, payload=payload)
        .on_conflict_do_nothing(index_elements=[TelegramUpdate.update_id])
        .returning(TelegramUpdate.update_id)
    )
    return result.scalar_one_or_none() is not None


async def queue_telegram_update(
    session: AsyncSession,
    update_id: int,
    payload: dict[str, Any],
) -> None:
    await session.execute(
        update(TelegramUpdate).where(TelegramUpdate.update_id == update_id).values(payload=payload)
    )
    await enqueue_job(
        session,
        kind="telegram.process_update",
        payload={"update_id": update_id},
        deduplication_key=f"telegram-update:{update_id}",
    )


async def mark_guest_uncertain(
    session: AsyncSession,
    update_id: int,
    error_code: str,
    *,
    actor_id: str,
) -> None:
    row = await session.get(TelegramUpdate, update_id)
    if row is None:
        return
    payload = dict(row.payload)
    payload["_dca_guest_answer_status"] = "uncertain"
    row.payload = payload
    await append_audit(
        session,
        event_type="telegram.guest_answer_uncertain",
        correlation_id=f"telegram-update:{update_id}",
        actor_type="system",
        actor_id=actor_id,
        outcome="uncertain",
        subject_type="telegram_update",
        subject_id=str(update_id),
        payload={"error_code": error_code},
    )


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
    if remainder and not (remainder[0].isspace() or remainder[0] in ",.:;!?—-"):
        return None
    return remainder.lstrip(" \t,.:;!?—-")


def extract_bot_call(value: str, *, username: str | None, first_name: str) -> str | None:
    if username:
        mention = extract_bot_mention(value, username)
        if mention is not None:
            return mention
    aliases = sorted({first_name.strip(), *BOT_NAME_ALIASES}, key=len, reverse=True)
    normalized = value.casefold()
    for alias in aliases:
        if not alias or not normalized.startswith(alias.casefold()):
            continue
        remainder = value[len(alias) :]
        if remainder and not (remainder[0].isspace() or remainder[0] in ",.:;!?—-"):
            continue
        return remainder.lstrip(" \t,.:;!?—-")
    return None


def document_requested(question: str) -> bool:
    return (
        DOCUMENT_ACTION_RE.search(question) is not None
        and DOCUMENT_SUBJECT_RE.search(question) is not None
    )


async def _message_context(
    session: Any,
    project: Project,
    user_id: UUID,
    message: Message,
    *,
    telegram_user_id: int,
) -> MessageContext:
    row = (
        await session.execute(
            select(User, ProjectMembership)
            .join(ProjectMembership, ProjectMembership.user_id == User.id)
            .where(
                User.id == user_id,
                User.active.is_(True),
                ProjectMembership.project_id == project.id,
            )
        )
    ).one_or_none()
    if row is None:
        raise ServiceError("project_scope_violation", "Project membership is unavailable")
    user, membership = row
    chat_id = await session.scalar(
        select(TelegramChat.id)
        .where(
            TelegramChat.project_id == project.id,
            TelegramChat.telegram_chat_id == message.chat.id,
            TelegramChat.enabled.is_(True),
            (
                (TelegramChat.message_thread_id == message.message_thread_id)
                | (TelegramChat.message_thread_id.is_(None))
            ),
        )
        .order_by(TelegramChat.message_thread_id.desc().nullslast())
    )
    if message.chat.type != ChatType.PRIVATE and chat_id is None:
        raise ServiceError(
            "chat_unavailable",
            "Этот чат не подключён к проекту. Добавьте его в whitelist панели.",
        )
    return MessageContext(
        project=project,
        user_id=user_id,
        chat_id=chat_id,
        requester_profile=project_member_profile(user, membership),
        telegram_user_id=telegram_user_id,
    )


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
        return await _message_context(
            session,
            project,
            identity.user_id,
            message,
            telegram_user_id=telegram_user_id,
        )

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
        return await _message_context(
            session,
            project,
            identity.user_id,
            message,
            telegram_user_id=telegram_user_id,
        )

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
        return await _message_context(
            session,
            projects[0],
            identity.user_id,
            message,
            telegram_user_id=telegram_user_id,
        )
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
    agent_settings = await load_project_agent_settings(session, context.project.id)
    guard_kinds = guard_request_kinds(question[:MAX_INTERACTION_QUESTION_CHARS])
    agent_role: Literal["knowledge", "bydlo_guard"] = (
        SECURITY_GUARD_ROLE if guard_kinds else "knowledge"
    )
    question_result = sanitize_text(
        question[:MAX_INTERACTION_QUESTION_CHARS],
        level="balanced",
        location="interaction.question",
    )
    safe_question = question_result.text[:MAX_INTERACTION_QUESTION_CHARS]
    conversation_thread_id: UUID | None = None
    if agent_settings.memory_enabled and agent_role == "knowledge":
        thread = await get_or_create_conversation_thread(
            session,
            project_id=context.project.id,
            chat_id=context.chat_id,
            user_id=context.user_id,
        )
        await append_conversation_message(
            session,
            project_id=context.project.id,
            chat_id=context.chat_id,
            user_id=context.user_id,
            thread_id=thread.id,
            role="user",
            source="telegram",
            content=safe_question,
            external_id=correlation_id,
            author_user_id=context.user_id,
        )
        conversation_thread_id = thread.id
    interaction = Interaction(
        project_id=context.project.id,
        repository_id=repository.id,
        conversation_thread_id=conversation_thread_id,
        correlation_id=correlation_id,
        source="telegram",
        source_ref={
            **source_ref,
            "telegram_user_id": context.telegram_user_id,
            "requester_user_id": str(context.user_id),
            "requester_profile": context.requester_profile,
            "question_privacy_findings": [dict(finding) for finding in question_result.findings],
            "agent_role": agent_role,
            "guard_kinds": list(guard_kinds),
        },
        question=safe_question,
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
        payload={
            "repository_id": str(repository.id),
            "commit": repository.current_commit,
            "input_privacy_findings": len(question_result.findings),
            "input_privacy_kinds": sorted(
                {finding["kind"] for finding in question_result.findings}
            ),
            "agent_role": agent_role,
        },
    )
    if agent_role == SECURITY_GUARD_ROLE:
        await append_audit(
            session,
            event_type="security.bydlo_guard_activated",
            correlation_id=f"{correlation_id}:guard",
            actor_type="user",
            actor_id=str(context.user_id),
            project_id=context.project.id,
            subject_type="interaction",
            subject_id=str(interaction.id),
            outcome="blocked",
            payload={
                "guard_role": SECURITY_GUARD_ROLE,
                "kinds": list(guard_kinds),
                "telegram_user_id": context.telegram_user_id,
            },
        )
    return interaction


def rich_answer_chunks(answer: str) -> list[str]:
    if len(answer) <= MAX_RICH_MESSAGE_CHARS:
        return [answer]

    chunks: list[str] = []
    offset = 0
    while len(answer) - offset > MAX_RICH_MESSAGE_CHARS:
        hard_end = offset + MAX_RICH_MESSAGE_CHARS
        paragraph_break = answer.rfind("\n\n", offset, hard_end)
        if paragraph_break > offset:
            end = paragraph_break + 2
        else:
            line_break = answer.rfind("\n", offset, hard_end)
            end = line_break + 1 if line_break > offset else hard_end
        chunks.append(answer[offset:end])
        offset = end
    chunks.append(answer[offset:])
    return chunks


def split_rich_answer(answer: str, *, attach_markdown: bool = True) -> tuple[str, str | None]:
    if len(answer) <= MAX_RICH_MESSAGE_CHARS:
        return answer, None
    preview = answer[: MAX_RICH_MESSAGE_CHARS - 220].rstrip()
    if attach_markdown:
        preview += "\n\nПолный ответ превышает лимит Rich Message и приложен файлом `answer.md`."
        return preview, answer
    preview += "\n\nПолный ответ сокращён по настройкам Telegram-вложений проекта."
    return preview, None


def markdown_documents(
    artifacts: list[dict[str, Any]],
    *,
    attachment: str | None,
) -> list[KnowledgeArtifact]:
    documents = [
        KnowledgeArtifact.model_validate({"name": item.get("name"), "content": item.get("content")})
        for item in artifacts
    ]
    names = {document.name.casefold() for document in documents}
    contents = {document.content for document in documents}
    if attachment is not None and attachment not in contents:
        name = "answer.md" if "answer.md" not in names else "full-answer.md"
        documents.insert(0, KnowledgeArtifact(name=name, content=attachment))
    return documents


def plain_rich_message(text: str) -> InputRichMessage:
    return InputRichMessage(html=html.escape(text), skip_entity_detection=True)


def knowledge_rich_message(interaction: Interaction, text: str) -> InputRichMessage:
    if interaction.source_ref.get("agent_role") == SECURITY_GUARD_ROLE:
        return plain_rich_message(text)
    return InputRichMessage(markdown=text, skip_entity_detection=True)


def draft_id_for_interaction(interaction_id: UUID) -> int:
    return interaction_id.int % 2_147_483_647 or 1
