from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatAction, ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SendMessage
from aiogram.types import InputRichBlockThinking, User

import dca.telegram as telegram_module
from dca.config import Settings
from dca.db import Database
from dca.telegram import (
    MAX_RICH_MESSAGE_CHARS,
    TelegramAdapter,
    document_requested,
    draft_id_for_interaction,
    extract_bot_call,
    extract_bot_mention,
    extract_project_prefix,
    ingest_telegram_update,
    markdown_documents,
    rich_answer_chunks,
    split_rich_answer,
)


@pytest.fixture
async def adapter() -> AsyncIterator[TelegramAdapter]:
    settings = Settings(database_url="postgresql+psycopg://dca:dca@localhost/dca")
    database = Database(settings)
    instance = TelegramAdapter(settings, database)
    try:
        yield instance
    finally:
        await instance.close()
        await database.close()


def bad_rich_message() -> TelegramBadRequest:
    return TelegramBadRequest(
        method=SendMessage(chat_id=1, text="test"),
        message="invalid rich message",
    )


def test_project_prefix_is_explicit_and_stable() -> None:
    project, question = extract_project_prefix("project:backend How does auth work?")
    assert project == "backend"
    assert question == "How does auth work?"
    assert extract_project_prefix("How does auth work?") == (None, "How does auth work?")


def test_bot_mention_is_exact_and_case_insensitive() -> None:
    assert extract_bot_mention("@DcaBot project:backend Вопрос", "dcabot") == (
        "project:backend Вопрос"
    )
    assert extract_bot_mention("@DcaBotExtra Вопрос", "dcabot") is None
    assert extract_bot_mention("Привет @DcaBot", "dcabot") is None


def test_bot_call_accepts_username_name_and_alias_only_at_the_start() -> None:
    assert (
        extract_bot_call(
            "@DcaBot, project:backend Вопрос",
            username="dcabot",
            first_name="Kakadu AI Agent",
        )
        == "project:backend Вопрос"
    )
    assert (
        extract_bot_call(
            "Kakadu AI Agent: как работает auth?",
            username="dcabot",
            first_name="Kakadu AI Agent",
        )
        == "как работает auth?"
    )
    assert (
        extract_bot_call(
            "Агентик, как работает auth?",
            username=None,
            first_name="Kakadu AI Agent",
        )
        == "как работает auth?"
    )
    assert (
        extract_bot_call(
            "АгентикExtra, вопрос",
            username="dcabot",
            first_name="Kakadu AI Agent",
        )
        is None
    )
    assert (
        extract_bot_call(
            "Привет, Агентик",
            username="dcabot",
            first_name="Kakadu AI Agent",
        )
        is None
    )


@pytest.mark.parametrize(
    "question",
    [
        "Создай документацию по интеграции",
        "Подготовь README.md с примером запуска",
        "Документацию по API оформи отдельным файлом",
        "Create a runbook for this deployment",
    ],
)
def test_document_requested_requires_explicit_creation_intent(question: str) -> None:
    assert document_requested(question)


@pytest.mark.parametrize(
    "question",
    [
        "Как работает интеграция?",
        "Где лежит README.md?",
        "Объясни существующую документацию",
        "Как создать .md файл?",
    ],
)
def test_document_requested_rejects_explanations_and_existing_files(question: str) -> None:
    assert not document_requested(question)


def test_long_rich_answer_falls_back_to_markdown_attachment() -> None:
    original = "x" * (MAX_RICH_MESSAGE_CHARS + 100)
    preview, attachment = split_rich_answer(original)
    assert len(preview) <= MAX_RICH_MESSAGE_CHARS
    assert attachment == original


def test_long_answer_chunks_preserve_text_and_prefer_paragraph_breaks() -> None:
    first = "a" * (MAX_RICH_MESSAGE_CHARS - 10)
    second = "b" * 100
    third = "c" * (MAX_RICH_MESSAGE_CHARS + 10)
    original = f"{first}\n\n{second}\n{third}"

    chunks = rich_answer_chunks(original)

    assert "".join(chunks) == original
    assert all(0 < len(chunk) <= MAX_RICH_MESSAGE_CHARS for chunk in chunks)
    assert chunks[0] == f"{first}\n\n"
    assert chunks[1] == f"{second}\n"


def test_explicit_markdown_artifacts_do_not_duplicate_long_answer() -> None:
    documents = markdown_documents(
        [{"name": "report.md", "content": "same answer"}],
        attachment="same answer",
    )

    assert [(document.name, document.content) for document in documents] == [
        ("report.md", "same answer")
    ]


def test_draft_id_is_never_zero() -> None:
    interaction_id = uuid4()
    draft_id = draft_id_for_interaction(interaction_id)

    assert draft_id == draft_id_for_interaction(interaction_id)
    assert 0 < draft_id < 2_147_483_647
    assert draft_id_for_interaction(type(interaction_id)(int=0)) == 1


@pytest.mark.asyncio
async def test_adapter_uses_configured_outbound_proxy() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://dca:dca@localhost/dca",
        outbound_proxy_url="http://proxy.example:8080",
    )
    database = Database(settings)
    adapter = TelegramAdapter(settings, database)
    try:
        assert isinstance(adapter.bot.session, AiohttpSession)
        assert adapter.bot.session.proxy == "http://proxy.example:8080/"
    finally:
        await adapter.close()
        await database.close()


@pytest.mark.asyncio
async def test_controlled_thinking_heartbeat_uses_the_interaction_draft_id(
    adapter: TelegramAdapter,
) -> None:
    adapter.bot.send_rich_message_draft = AsyncMock(return_value=True)  # type: ignore[method-assign]
    interaction = SimpleNamespace(
        id=uuid4(),
        source_ref={
            "delivery": {
                "kind": "private_draft",
                "chat_id": 42,
                "message_thread_id": 7,
            }
        },
    )

    await adapter.send_knowledge_progress(interaction)
    await adapter.send_knowledge_progress(interaction)

    calls = adapter.bot.send_rich_message_draft.await_args_list
    assert len(calls) == 2
    assert {call.kwargs["draft_id"] for call in calls} == {draft_id_for_interaction(interaction.id)}
    for call in calls:
        blocks = call.kwargs["rich_message"].blocks
        assert blocks is not None
        assert len(blocks) == 1
        assert isinstance(blocks[0], InputRichBlockThinking)
        assert blocks[0].text == telegram_module.KNOWLEDGE_PROGRESS_TEXT


@pytest.mark.asyncio
async def test_private_stream_uses_thinking_block_and_one_stable_draft_id(
    adapter: TelegramAdapter,
) -> None:
    adapter.bot.send_rich_message_draft = AsyncMock(return_value=True)  # type: ignore[method-assign]
    interaction = SimpleNamespace(
        id=uuid4(),
        source_ref={
            "delivery": {
                "kind": "private_draft",
                "chat_id": 42,
                "message_thread_id": 7,
            }
        },
    )

    await adapter.send_knowledge_stream(
        interaction,
        answer_markdown="",
        thinking="Проверяю реализацию",
    )
    await adapter.send_knowledge_stream(
        interaction,
        answer_markdown="**Проверенный ответ**",
        thinking="Проверяю реализацию",
    )

    calls = adapter.bot.send_rich_message_draft.await_args_list
    assert len(calls) == 2
    assert {call.kwargs["draft_id"] for call in calls} == {draft_id_for_interaction(interaction.id)}
    assert all(call.kwargs["chat_id"] == 42 for call in calls)
    assert all(call.kwargs["message_thread_id"] == 7 for call in calls)
    first_rich = calls[0].kwargs["rich_message"]
    assert first_rich.blocks is not None
    assert isinstance(first_rich.blocks[0], InputRichBlockThinking)
    assert first_rich.blocks[0].text == "Проверяю реализацию"
    second_markdown = calls[1].kwargs["rich_message"].markdown
    assert second_markdown is not None
    assert "<tg-thinking>Проверяю реализацию</tg-thinking>" in second_markdown
    assert "**Проверенный ответ**" in second_markdown


@pytest.mark.asyncio
async def test_thinking_draft_falls_back_to_native_empty_draft(
    adapter: TelegramAdapter,
) -> None:
    adapter.bot.send_rich_message_draft = AsyncMock(  # type: ignore[method-assign]
        side_effect=bad_rich_message()
    )
    adapter.bot.send_message_draft = AsyncMock(return_value=True)  # type: ignore[method-assign]
    interaction = SimpleNamespace(
        id=uuid4(),
        source_ref={"delivery": {"kind": "private_draft", "chat_id": 42}},
    )

    await adapter.send_knowledge_progress(interaction)

    adapter.bot.send_message_draft.assert_awaited_once_with(
        chat_id=42,
        message_thread_id=None,
        draft_id=draft_id_for_interaction(interaction.id),
        text="",
    )


@pytest.mark.asyncio
async def test_group_progress_and_stream_use_typing_action(adapter: TelegramAdapter) -> None:
    adapter.bot.send_chat_action = AsyncMock(return_value=True)  # type: ignore[method-assign]
    adapter.bot.send_rich_message_draft = AsyncMock(return_value=True)  # type: ignore[method-assign]
    adapter.bot.send_message_draft = AsyncMock(return_value=True)  # type: ignore[method-assign]
    interaction = SimpleNamespace(
        id=uuid4(),
        source_ref={
            "delivery": {
                "kind": "group_message",
                "chat_id": -100,
                "message_id": 9,
                "message_thread_id": 7,
            }
        },
    )

    await adapter.send_knowledge_progress(interaction)
    await adapter.send_knowledge_stream(
        interaction,
        answer_markdown="Промежуточный ответ",
        thinking="Проверяю",
    )

    assert adapter.bot.send_chat_action.await_count == 2
    for call in adapter.bot.send_chat_action.await_args_list:
        assert call.kwargs == {
            "chat_id": -100,
            "message_thread_id": 7,
            "action": ChatAction.TYPING,
        }
    adapter.bot.send_rich_message_draft.assert_not_awaited()
    adapter.bot.send_message_draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_final_retries_as_plain_html_without_losing_answer(
    adapter: TelegramAdapter,
) -> None:
    sent = SimpleNamespace(chat=SimpleNamespace(id=42))
    adapter.bot.send_rich_message = AsyncMock(  # type: ignore[method-assign]
        side_effect=[bad_rich_message(), sent]
    )
    answer = "Ответ <важный> & проверенный"
    interaction = SimpleNamespace(
        source_ref={
            "delivery": {
                "kind": "private_draft",
                "chat_id": 42,
                "message_thread_id": 7,
            }
        }
    )

    await adapter.publish_knowledge_answer(interaction, answer)

    calls = adapter.bot.send_rich_message.await_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["rich_message"].markdown == answer
    assert calls[1].kwargs["rich_message"].html == ("Ответ &lt;важный&gt; &amp; проверенный")


@pytest.mark.asyncio
async def test_private_long_answer_publishes_every_chunk_without_document(
    adapter: TelegramAdapter,
) -> None:
    sent = SimpleNamespace(chat=SimpleNamespace(id=42))
    answer = f"{'a' * (MAX_RICH_MESSAGE_CHARS - 2)}\n\n{'b' * 100}"
    chunks = rich_answer_chunks(answer)
    adapter.bot.send_rich_message = AsyncMock(  # type: ignore[method-assign]
        side_effect=[bad_rich_message(), sent, bad_rich_message(), sent]
    )
    adapter.bot.send_document = AsyncMock(return_value=True)  # type: ignore[method-assign]
    interaction = SimpleNamespace(
        source_ref={
            "delivery": {
                "kind": "private_draft",
                "chat_id": 42,
                "message_thread_id": 7,
            }
        }
    )

    await adapter.publish_knowledge_answer(
        interaction,
        answer,
        attach_markdown=False,
    )

    calls = adapter.bot.send_rich_message.await_args_list
    assert len(chunks) == 2
    assert [calls[0].kwargs["rich_message"].markdown, calls[2].kwargs["rich_message"].markdown] == (
        chunks
    )
    assert calls[1].kwargs["rich_message"].html == telegram_module.html.escape(chunks[0])
    assert calls[3].kwargs["rich_message"].html == telegram_module.html.escape(chunks[1])
    assert all(call.kwargs["message_thread_id"] == 7 for call in calls)
    adapter.bot.send_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_long_answer_edits_first_chunk_and_sends_the_rest(
    adapter: TelegramAdapter,
) -> None:
    sent = SimpleNamespace(chat=SimpleNamespace(id=-100))
    answer = f"{'a' * (MAX_RICH_MESSAGE_CHARS - 2)}\n\n{'b' * 100}"
    chunks = rich_answer_chunks(answer)
    adapter.bot.edit_message_text = AsyncMock(  # type: ignore[method-assign]
        side_effect=[bad_rich_message(), True]
    )
    adapter.bot.send_rich_message = AsyncMock(  # type: ignore[method-assign]
        side_effect=[bad_rich_message(), sent]
    )
    adapter.bot.send_document = AsyncMock(return_value=True)  # type: ignore[method-assign]
    interaction = SimpleNamespace(
        source_ref={
            "delivery": {
                "kind": "group_message",
                "chat_id": -100,
                "message_id": 9,
                "message_thread_id": 7,
            }
        }
    )

    await adapter.publish_knowledge_answer(
        interaction,
        answer,
        attach_markdown=False,
    )

    edit_calls = adapter.bot.edit_message_text.await_args_list
    send_calls = adapter.bot.send_rich_message.await_args_list
    assert len(chunks) == 2
    assert edit_calls[0].kwargs["rich_message"].markdown == chunks[0]
    assert edit_calls[1].kwargs["rich_message"].html == telegram_module.html.escape(chunks[0])
    assert send_calls[0].kwargs["rich_message"].markdown == chunks[1]
    assert send_calls[1].kwargs["rich_message"].html == telegram_module.html.escape(chunks[1])
    assert all(call.kwargs["chat_id"] == -100 for call in send_calls)
    assert all(call.kwargs["message_thread_id"] == 7 for call in send_calls)
    adapter.bot.send_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_answer_sends_explicit_markdown_when_enabled(
    adapter: TelegramAdapter,
) -> None:
    sent = SimpleNamespace(chat=SimpleNamespace(id=42))
    adapter.bot.send_rich_message = AsyncMock(return_value=sent)  # type: ignore[method-assign]
    adapter.bot.send_document = AsyncMock(return_value=True)  # type: ignore[method-assign]
    interaction = SimpleNamespace(source_ref={"delivery": {"kind": "private_draft", "chat_id": 42}})

    await adapter.publish_knowledge_answer(
        interaction,
        "Короткий ответ",
        artifacts=[{"name": "runbook.md", "content": "# Runbook"}],
        attach_markdown=True,
    )

    document = adapter.bot.send_document.await_args.kwargs["document"]
    assert document.filename == "runbook.md"
    assert document.data == b"# Runbook"


@pytest.mark.asyncio
async def test_agent_text_message_uses_rich_markdown_with_plain_fallback(
    adapter: TelegramAdapter,
) -> None:
    sent = SimpleNamespace(message_id=71)
    adapter.bot.send_rich_message = AsyncMock(  # type: ignore[method-assign]
        side_effect=bad_rich_message()
    )
    adapter.bot.send_message = AsyncMock(return_value=sent)  # type: ignore[method-assign]

    message_id = await adapter.deliver_agent_message(
        chat_id=-100,
        message_thread_id=7,
        text_markdown="**Готово**",
        attachment_name=None,
        attachment_markdown=None,
    )

    assert message_id == 71
    rich = adapter.bot.send_rich_message.await_args.kwargs["rich_message"]
    assert rich.markdown == "**Готово**"
    adapter.bot.send_message.assert_awaited_once_with(
        chat_id=-100,
        message_thread_id=7,
        text="**Готово**",
    )


@pytest.mark.asyncio
async def test_agent_attachment_never_silently_truncates_caption(
    adapter: TelegramAdapter,
) -> None:
    adapter.bot.send_document = AsyncMock(return_value=True)  # type: ignore[method-assign]

    with pytest.raises(telegram_module.ServiceError, match="1024"):
        await adapter.deliver_agent_message(
            chat_id=-100,
            message_thread_id=None,
            text_markdown="x" * 1_025,
            attachment_name="report.md",
            attachment_markdown="# Report",
        )

    adapter.bot.send_document.assert_not_awaited()


@pytest.mark.parametrize(
    "delivery, expected_key, expected_value",
    [
        ({"kind": "guest", "inline_message_id": "inline-7"}, "inline_message_id", "inline-7"),
        (
            {"kind": "group_message", "chat_id": -100, "message_id": 15},
            "message_id",
            15,
        ),
    ],
    ids=["guest", "group"],
)
@pytest.mark.asyncio
async def test_guest_and_group_edits_retry_as_plain_html(
    adapter: TelegramAdapter,
    delivery: dict[str, object],
    expected_key: str,
    expected_value: object,
) -> None:
    adapter.bot.edit_message_text = AsyncMock(  # type: ignore[method-assign]
        side_effect=[bad_rich_message(), True]
    )
    interaction = SimpleNamespace(source_ref={"delivery": delivery})

    await adapter.publish_knowledge_answer(interaction, "Ответ <code> & ссылка")

    calls = adapter.bot.edit_message_text.await_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["rich_message"].markdown == "Ответ <code> & ссылка"
    assert calls[1].kwargs["rich_message"].html == "Ответ &lt;code&gt; &amp; ссылка"
    assert calls[1].kwargs[expected_key] == expected_value


@pytest.mark.asyncio
async def test_guest_placeholder_uses_answer_guest_query(adapter: TelegramAdapter) -> None:
    adapter.bot.answer_guest_query = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(inline_message_id="inline-9")
    )
    payload = {
        "update_id": 9,
        "guest_message": {
            "message_id": 1,
            "date": 0,
            "chat": {"id": -100, "type": "group", "title": "Developers"},
            "guest_query_id": "guest-9",
            "text": "project:backend вопрос",
            "guest_bot_caller_user": {
                "id": 777,
                "is_bot": True,
                "first_name": "Caller",
            },
        },
    }

    inline_id = await adapter.answer_guest_placeholder(payload)

    assert inline_id == "inline-9"
    call = adapter.bot.answer_guest_query.await_args
    assert call.kwargs["guest_query_id"] == "guest-9"
    assert call.kwargs["result"].input_message_content.rich_message.markdown is not None


@pytest.mark.asyncio
async def test_shared_ingest_keeps_guest_uncertain_semantics(monkeypatch) -> None:
    reserve = AsyncMock(return_value=True)
    queue = AsyncMock()
    uncertain = AsyncMock()
    monkeypatch.setattr(telegram_module, "reserve_telegram_update", reserve)
    monkeypatch.setattr(telegram_module, "queue_telegram_update", queue)
    monkeypatch.setattr(telegram_module, "mark_guest_uncertain", uncertain)
    telegram = SimpleNamespace(
        answer_guest_placeholder=AsyncMock(side_effect=RuntimeError("delivery unknown"))
    )
    session = object()
    payload = {"update_id": 19, "guest_message": {"guest_query_id": "guest-19"}}

    assert await ingest_telegram_update(
        session,
        telegram,
        payload,
        actor_id="polling-worker",  # type: ignore[arg-type]
    )

    uncertain.assert_awaited_once_with(session, 19, "RuntimeError", actor_id="polling-worker")
    queue.assert_not_awaited()


@pytest.mark.asyncio
async def test_ephemeral_command_and_answer_keep_receiver_scope(
    adapter: TelegramAdapter,
) -> None:
    adapter.bot.set_my_commands = AsyncMock(return_value=True)  # type: ignore[method-assign]
    adapter.bot.edit_ephemeral_message_text = AsyncMock(return_value=True)  # type: ignore[method-assign]

    await adapter.setup_commands()
    group_commands = adapter.bot.set_my_commands.await_args_list[1].args[0]
    assert {command.command for command in group_commands if command.is_ephemeral} == {
        "ask_private",
        "request",
    }

    interaction = SimpleNamespace(
        source_ref={
            "delivery": {
                "kind": "ephemeral",
                "chat_id": -100,
                "receiver_user_id": 777,
                "ephemeral_message_id": 44,
            }
        }
    )
    await adapter.publish_knowledge_answer(interaction, "Приватный ответ")
    adapter.bot.edit_ephemeral_message_text.assert_awaited_once_with(
        chat_id=-100,
        receiver_user_id=777,
        ephemeral_message_id=44,
        text="Приватный ответ",
    )


@pytest.mark.asyncio
async def test_knowledge_error_never_exposes_internal_failure_detail(
    adapter: TelegramAdapter,
) -> None:
    adapter.bot.send_message = AsyncMock(return_value=True)  # type: ignore[method-assign]
    interaction_id = uuid4()
    interaction = SimpleNamespace(
        id=interaction_id,
        source_ref={"delivery": {"kind": "private_draft", "chat_id": 42}},
    )

    await adapter.publish_knowledge_error(interaction)

    text = adapter.bot.send_message.await_args.kwargs["text"]
    assert str(interaction_id)[:8] in text
    assert "stderr" not in text.casefold()
    assert "token" not in text.casefold()


@pytest.mark.parametrize(
    "prefer_ephemeral, incoming_ephemeral_id, expected_kind",
    [(False, None, "group_message"), (True, 88, "ephemeral")],
    ids=["group-placeholder", "ephemeral-placeholder"],
)
@pytest.mark.asyncio
async def test_group_and_ephemeral_questions_create_scoped_placeholders(
    adapter: TelegramAdapter,
    monkeypatch: pytest.MonkeyPatch,
    prefer_ephemeral: bool,
    incoming_ephemeral_id: int | None,
    expected_kind: str,
) -> None:
    @asynccontextmanager
    async def session() -> AsyncIterator[object]:
        yield object()

    monkeypatch.setattr(adapter.database, "session", session)
    monkeypatch.setattr(
        telegram_module,
        "resolve_context",
        AsyncMock(return_value=SimpleNamespace(project=SimpleNamespace(id="p"), user_id="u")),
    )
    monkeypatch.setattr(
        telegram_module,
        "load_project_agent_settings",
        AsyncMock(
            return_value=SimpleNamespace(
                enabled=True,
                telegram_private_mode="all_messages",
                telegram_group_mode="mentions",
            )
        ),
    )
    queued = AsyncMock()
    monkeypatch.setattr(telegram_module, "queue_interaction", queued)
    placeholder = SimpleNamespace(
        chat=SimpleNamespace(id=-100),
        message_id=15,
        ephemeral_message_id=44 if prefer_ephemeral else None,
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-100, type=ChatType.SUPERGROUP),
        from_user=SimpleNamespace(id=777),
        ephemeral_message_id=incoming_ephemeral_id,
        message_thread_id=7,
        answer=AsyncMock(return_value=placeholder),
    )
    adapter.bot.send_message = AsyncMock(return_value=placeholder)  # type: ignore[method-assign]

    await adapter._queue_code_question(
        message=message,
        event_update=SimpleNamespace(update_id=23),
        question="Как работает токен?",
        explicit_project_slug="backend",
        prefer_ephemeral=prefer_ephemeral,
    )

    delivery = queued.await_args.kwargs["source_ref"]["delivery"]
    assert delivery["kind"] == expected_kind
    if prefer_ephemeral:
        call = adapter.bot.send_message.await_args
        assert call.kwargs["receiver_user_id"] == 777
        assert call.kwargs["reply_parameters"].ephemeral_message_id == 88
        assert delivery["ephemeral_message_id"] == 44
        message.answer.assert_not_awaited()
    else:
        message.answer.assert_awaited_once()
        adapter.bot.send_message.assert_not_awaited()
        assert delivery["message_id"] == 15


@pytest.mark.parametrize(
    "text",
    [
        "@DcaBot project:backend Где проверяется токен?",
        "Агентик, project:backend Где проверяется токен?",
        "Kakadu AI Agent: project:backend Где проверяется токен?",
    ],
    ids=["username", "alias", "bot-name"],
)
@pytest.mark.asyncio
async def test_group_bot_call_queues_question_with_explicit_project(
    adapter: TelegramAdapter,
    text: str,
) -> None:
    adapter.bot.me = AsyncMock(  # type: ignore[method-assign]
        return_value=User(
            id=999,
            is_bot=True,
            first_name="Kakadu AI Agent",
            username="DcaBot",
        )
    )
    adapter._queue_code_question = AsyncMock()  # type: ignore[method-assign]
    payload = {
        "update_id": 17,
        "message": {
            "message_id": 3,
            "date": 0,
            "chat": {"id": -100, "type": "supergroup", "title": "Developers"},
            "from": {"id": 777, "is_bot": False, "first_name": "Dev"},
            "text": text,
        },
    }

    await adapter.process_raw_update(payload)

    adapter._queue_code_question.assert_awaited_once()  # type: ignore[attr-defined]
    call = adapter._queue_code_question.await_args  # type: ignore[attr-defined]
    assert call.kwargs["question"] == "Где проверяется токен?"
    assert call.kwargs["explicit_project_slug"] == "backend"
    assert call.kwargs["prefer_ephemeral"] is False


@pytest.mark.asyncio
async def test_plain_text_uses_all_messages_mode_without_command_path(
    adapter: TelegramAdapter,
) -> None:
    adapter.bot.me = AsyncMock(  # type: ignore[method-assign]
        return_value=User(
            id=999,
            is_bot=True,
            first_name="Kakadu AI Agent",
            username="DcaBot",
        )
    )
    adapter._queue_code_question = AsyncMock()  # type: ignore[method-assign]
    payload = {
        "update_id": 18,
        "message": {
            "message_id": 4,
            "date": 0,
            "chat": {"id": -100, "type": "supergroup", "title": "Developers"},
            "from": {"id": 777, "is_bot": False, "first_name": "Dev"},
            "text": "project:backend Где хранится конфиг?",
        },
    }

    await adapter.process_raw_update(payload)

    call = adapter._queue_code_question.await_args  # type: ignore[attr-defined]
    assert call.kwargs["question"] == "Где хранится конфиг?"
    assert call.kwargs["allowed_modes"] == {"all_messages"}


@pytest.mark.parametrize(
    "text",
    [
        "/ask_private project:backend секретный вопрос",
        "/request project:backend bug Секрет | Приватные детали",
    ],
    ids=["ask-private", "request"],
)
@pytest.mark.asyncio
async def test_private_group_commands_never_fall_back_to_public_message(
    adapter: TelegramAdapter,
    text: str,
) -> None:
    adapter.bot.send_message = AsyncMock(return_value=SimpleNamespace())  # type: ignore[method-assign]
    payload = {
        "update_id": 21,
        "message": {
            "message_id": 4,
            "date": 0,
            "chat": {"id": -100, "type": "supergroup", "title": "Developers"},
            "from": {"id": 777, "is_bot": False, "first_name": "Dev"},
            "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}],
        },
    }

    await adapter.process_raw_update(payload)

    adapter.bot.send_message.assert_awaited_once()
    call = adapter.bot.send_message.await_args
    assert call.kwargs["chat_id"] == 777
    assert "недоступна" in call.kwargs["text"]
    assert "секрет" not in call.kwargs["text"].casefold()


@pytest.mark.asyncio
async def test_private_group_command_stays_silent_when_dm_is_forbidden(
    adapter: TelegramAdapter,
) -> None:
    adapter.bot.send_message = AsyncMock(  # type: ignore[method-assign]
        side_effect=TelegramForbiddenError(
            method=SendMessage(chat_id=777, text="test"),
            message="bot can't initiate conversation",
        )
    )
    payload = {
        "update_id": 22,
        "message": {
            "message_id": 5,
            "date": 0,
            "chat": {"id": -100, "type": "supergroup", "title": "Developers"},
            "from": {"id": 777, "is_bot": False, "first_name": "Dev"},
            "text": "/ask_private секретный вопрос",
            "entities": [{"type": "bot_command", "offset": 0, "length": 12}],
        },
    }

    await adapter.process_raw_update(payload)

    adapter.bot.send_message.assert_awaited_once()
    assert adapter.bot.send_message.await_args.kwargs["chat_id"] == 777


@pytest.mark.asyncio
async def test_private_start_verifies_rebound_identity(
    adapter: TelegramAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = SimpleNamespace(
        telegram_user_id=777,
        private_chat_id=None,
        reachable=False,
        verified_at=None,
        username=None,
    )

    class FakeSession:
        async def scalar(self, _statement: object) -> object:
            return identity

    @asynccontextmanager
    async def session() -> AsyncIterator[FakeSession]:
        yield FakeSession()

    monkeypatch.setattr(adapter.database, "session", session)
    adapter.bot.session = AsyncMock(return_value=SimpleNamespace())  # type: ignore[assignment]
    payload = {
        "update_id": 24,
        "message": {
            "message_id": 6,
            "date": 0,
            "chat": {"id": 777, "type": "private", "first_name": "Dev"},
            "from": {
                "id": 777,
                "is_bot": False,
                "first_name": "Dev",
                "username": "frontend_dev",
            },
            "text": "/start",
            "entities": [{"type": "bot_command", "offset": 0, "length": 6}],
        },
    }

    await adapter.process_raw_update(payload)

    assert identity.private_chat_id == 777
    assert identity.reachable is True
    assert identity.verified_at is not None
    assert identity.username == "frontend_dev"
