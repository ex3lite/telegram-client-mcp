from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from dca.db import ConversationMemory, ConversationMessage, ConversationThread
from dca.memory import (
    _bound_context,
    _sanitize_for_memory,
    get_or_create_conversation_thread,
    load_conversation_context,
)
from dca.service import ServiceError


class InsertResult:
    def __init__(self, value: UUID) -> None:
        self.value = value

    def scalar_one_or_none(self) -> UUID:
        return self.value


@pytest.mark.asyncio
async def test_user_only_private_thread_is_supported() -> None:
    project_id = uuid4()
    user_id = uuid4()

    class UserOnlySession:
        def __init__(self) -> None:
            self.thread: ConversationThread | None = None

        async def scalar(self, statement: object) -> object | None:
            sql = str(statement)
            if "project_memberships" in sql:
                return user_id
            return self.thread

        async def execute(self, statement: object) -> InsertResult:
            params = statement.compile().params  # type: ignore[union-attr]
            self.thread = ConversationThread(
                id=params["id"],
                project_id=params["project_id"],
                chat_id=params["chat_id"],
                user_id=params["user_id"],
            )
            return InsertResult(self.thread.id)

    thread = await get_or_create_conversation_thread(
        UserOnlySession(),  # type: ignore[arg-type]
        project_id=project_id,
        chat_id=None,
        user_id=user_id,
    )

    assert thread.project_id == project_id
    assert thread.chat_id is None
    assert thread.user_id == user_id


@pytest.mark.asyncio
async def test_context_lookup_fails_closed_across_projects() -> None:
    class WrongProjectSession:
        scalars_called = False

        async def scalar(self, _: object) -> None:
            return None

        async def scalars(self, _: object) -> None:
            self.scalars_called = True
            raise AssertionError("messages must not be read before scope verification")

    session = WrongProjectSession()
    with pytest.raises(ServiceError) as error:
        await load_conversation_context(
            session,  # type: ignore[arg-type]
            project_id=uuid4(),
            chat_id=None,
            user_id=uuid4(),
            thread_id=uuid4(),
        )

    assert error.value.code == "conversation_scope_unavailable"
    assert session.scalars_called is False


def test_memory_redacts_secrets_and_context_keeps_newest_within_budget() -> None:
    raw_value = "Bearer abcdefghijklmnopqrstuvwxyz"
    safe, findings = _sanitize_for_memory(raw_value, "memory.test")
    assert raw_value not in safe
    assert safe == "[REDACTED:bearer_token]"
    assert findings == [{"kind": "bearer_token", "location": "memory.test", "action": "redacted"}]

    now = datetime.now(UTC)
    thread_id = uuid4()
    project_id = uuid4()
    summary = ConversationMemory(
        project_id=project_id,
        thread_id=thread_id,
        kind="summary",
        memory_key="current",
        content="s" * 1_000,
        created_at=now,
        updated_at=now,
    )
    fact = ConversationMemory(
        project_id=project_id,
        thread_id=thread_id,
        kind="fact",
        memory_key="profile.stack",
        content="f" * 1_000,
        created_at=now,
        updated_at=now,
    )
    messages = [
        ConversationMessage(
            project_id=project_id,
            thread_id=thread_id,
            role="user",
            source="telegram",
            author_user_id=uuid4(),
            content=value * 2_000,
            created_at=now,
            updated_at=now,
        )
        for value in ("a", "b")
    ]

    context = _bound_context(
        thread_id=thread_id,
        summary=summary,
        facts=[fact],
        messages=messages,
        max_chars=3_000,
    )

    assert len(context.messages) == 1
    assert context.messages[0].content.startswith("b")
    assert (
        len(context.summary or "")
        + sum(len(item.key) + len(item.content) + 2 for item in context.facts)
        + sum(len(item.content) for item in context.messages)
        <= 3_000
    )
