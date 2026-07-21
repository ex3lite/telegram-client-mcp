from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SendMessage
from aiogram.types import User

import dca.telegram as telegram_module
from dca.config import Settings
from dca.db import Database
from dca.telegram import (
    MAX_RICH_MESSAGE_CHARS,
    TelegramAdapter,
    extract_bot_mention,
    extract_project_prefix,
    ingest_telegram_update,
    new_draft_id,
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


def test_long_rich_answer_falls_back_to_markdown_attachment() -> None:
    original = "x" * (MAX_RICH_MESSAGE_CHARS + 100)
    preview, attachment = split_rich_answer(original)
    assert len(preview) <= MAX_RICH_MESSAGE_CHARS
    assert attachment == original


def test_draft_id_is_never_zero() -> None:
    assert all(new_draft_id() > 0 for _ in range(100))


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
async def test_private_progress_uses_rich_draft_with_stable_id(
    adapter: TelegramAdapter,
) -> None:
    adapter.bot.send_rich_message_draft = AsyncMock(return_value=True)  # type: ignore[method-assign]
    interaction = SimpleNamespace(
        source_ref={
            "delivery": {
                "kind": "private_draft",
                "chat_id": 42,
                "message_thread_id": 7,
            }
        }
    )
    await adapter.send_knowledge_progress(interaction, draft_id=91)
    await adapter.send_knowledge_progress(interaction, draft_id=91)
    assert adapter.bot.send_rich_message_draft.await_count == 2
    for call in adapter.bot.send_rich_message_draft.await_args_list:
        assert call.kwargs["draft_id"] == 91
        rich = call.kwargs["rich_message"]
        assert rich.blocks is not None
        assert rich.markdown is None


@pytest.mark.asyncio
async def test_private_progress_retries_as_plain_html(adapter: TelegramAdapter) -> None:
    adapter.bot.send_rich_message_draft = AsyncMock(  # type: ignore[method-assign]
        side_effect=[bad_rich_message(), True]
    )
    interaction = SimpleNamespace(source_ref={"delivery": {"kind": "private_draft", "chat_id": 42}})

    await adapter.send_knowledge_progress(interaction, draft_id=92)

    calls = adapter.bot.send_rich_message_draft.await_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["rich_message"].blocks is not None
    assert calls[1].kwargs["rich_message"].html == "Проверяю код и подтверждаю ссылки"
    assert calls[1].kwargs["draft_id"] == 92


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


@pytest.mark.asyncio
async def test_group_mention_queues_question_with_explicit_project(
    adapter: TelegramAdapter,
) -> None:
    adapter.bot.me = AsyncMock(  # type: ignore[method-assign]
        return_value=User(id=999, is_bot=True, first_name="DCA", username="DcaBot")
    )
    adapter._queue_code_question = AsyncMock()  # type: ignore[method-assign]
    payload = {
        "update_id": 17,
        "message": {
            "message_id": 3,
            "date": 0,
            "chat": {"id": -100, "type": "supergroup", "title": "Developers"},
            "from": {"id": 777, "is_bot": False, "first_name": "Dev"},
            "text": "@DcaBot project:backend Где проверяется токен?",
        },
    }

    await adapter.process_raw_update(payload)

    adapter._queue_code_question.assert_awaited_once()  # type: ignore[attr-defined]
    call = adapter._queue_code_question.await_args  # type: ignore[attr-defined]
    assert call.kwargs["question"] == "Где проверяется токен?"
    assert call.kwargs["explicit_project_slug"] == "backend"
    assert call.kwargs["prefer_ephemeral"] is False


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
