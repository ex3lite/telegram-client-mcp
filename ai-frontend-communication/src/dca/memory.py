from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from dca.db import (
    ConversationMemory,
    ConversationMessage,
    ConversationThread,
    ProjectMembership,
    TelegramChat,
)
from dca.domain import utcnow
from dca.privacy import PrivacyFinding, sanitize_text
from dca.service import ServiceError

MessageRole = Literal["user", "assistant", "agent", "tool"]
MemoryKind = Literal["summary", "fact"]

_MESSAGE_ROLES = {"user", "assistant", "agent", "tool"}
_SOURCE_RE = re.compile(r"[a-z][a-z0-9_.-]{1,31}")
_MEMORY_KEY_RE = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,127}")


@dataclass(frozen=True, slots=True)
class ConversationFact:
    key: str
    content: str


@dataclass(frozen=True, slots=True)
class ConversationContextMessage:
    role: str
    source: str
    content: str
    author_user_id: UUID | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ConversationContext:
    thread_id: UUID
    summary: str | None
    facts: tuple[ConversationFact, ...]
    messages: tuple[ConversationContextMessage, ...]


async def get_or_create_conversation_thread(
    session: AsyncSession,
    *,
    project_id: UUID,
    chat_id: UUID | None,
    user_id: UUID | None,
) -> ConversationThread:
    await _require_chat_scope(session, project_id=project_id, chat_id=chat_id, user_id=user_id)
    existing = await _find_thread(
        session,
        project_id=project_id,
        chat_id=chat_id,
        user_id=user_id,
    )
    if existing is not None:
        return existing

    thread_id = uuid4()
    inserted = (
        await session.execute(
            insert(ConversationThread)
            .values(
                id=thread_id,
                project_id=project_id,
                chat_id=chat_id,
                user_id=user_id,
            )
            .on_conflict_do_nothing(constraint="uq_conversation_thread_scope")
            .returning(ConversationThread.id)
        )
    ).scalar_one_or_none()
    thread = await _find_thread(
        session,
        project_id=project_id,
        chat_id=chat_id,
        user_id=user_id,
    )
    if thread is None:
        raise ServiceError("internal_error", "Conversation thread was not persisted")
    if inserted is not None and thread.id != inserted:
        raise ServiceError("internal_error", "Conversation thread identity changed")
    return thread


async def find_conversation_thread(
    session: AsyncSession,
    *,
    project_id: UUID,
    chat_id: UUID | None,
    user_id: UUID | None,
) -> ConversationThread | None:
    """Read one validated scope without creating panel-visible state."""
    await _require_chat_scope(session, project_id=project_id, chat_id=chat_id, user_id=user_id)
    return await _find_thread(
        session,
        project_id=project_id,
        chat_id=chat_id,
        user_id=user_id,
    )


async def append_conversation_message(
    session: AsyncSession,
    *,
    project_id: UUID,
    chat_id: UUID | None,
    user_id: UUID | None,
    thread_id: UUID,
    role: MessageRole,
    source: str,
    content: str,
    external_id: str | None = None,
    author_user_id: UUID | None = None,
) -> tuple[ConversationMessage, bool]:
    _validate_message(role=role, source=source, content=content, external_id=external_id)
    safe_content, findings = _sanitize_for_memory(content, "conversation_message.content")
    thread = await _require_thread_scope(
        session,
        project_id=project_id,
        chat_id=chat_id,
        user_id=user_id,
        thread_id=thread_id,
    )
    await _require_message_author(
        session,
        project_id=project_id,
        thread_user_id=thread.user_id,
        role=role,
        author_user_id=author_user_id,
    )

    if external_id is not None:
        existing = await session.scalar(
            select(ConversationMessage).where(
                ConversationMessage.thread_id == thread_id,
                ConversationMessage.source == source,
                ConversationMessage.external_id == external_id,
            )
        )
        if existing is not None:
            _ensure_same_message(
                existing,
                project_id=project_id,
                role=role,
                author_user_id=author_user_id,
                content=safe_content,
            )
            return existing, False

    message_id = uuid4()
    inserted = (
        await session.execute(
            insert(ConversationMessage)
            .values(
                id=message_id,
                project_id=project_id,
                thread_id=thread_id,
                role=role,
                source=source,
                external_id=external_id,
                author_user_id=author_user_id,
                content=safe_content,
                privacy_findings=findings,
            )
            .on_conflict_do_nothing(constraint="uq_conversation_message_source")
            .returning(ConversationMessage.id)
        )
    ).scalar_one_or_none()
    if inserted is None:
        raced = await session.scalar(
            select(ConversationMessage).where(
                ConversationMessage.thread_id == thread_id,
                ConversationMessage.source == source,
                ConversationMessage.external_id == external_id,
            )
        )
        if raced is None:
            raise ServiceError("internal_error", "Message idempotency conflict was unresolved")
        _ensure_same_message(
            raced,
            project_id=project_id,
            role=role,
            author_user_id=author_user_id,
            content=safe_content,
        )
        return raced, False

    message = await session.get(ConversationMessage, message_id)
    if message is None:
        raise ServiceError("internal_error", "Conversation message was not persisted")
    thread.last_message_at = utcnow()
    return message, True


async def upsert_conversation_memory(
    session: AsyncSession,
    *,
    project_id: UUID,
    chat_id: UUID | None,
    user_id: UUID | None,
    thread_id: UUID,
    kind: MemoryKind,
    memory_key: str,
    content: str,
) -> ConversationMemory:
    _validate_memory(kind=kind, memory_key=memory_key, content=content)
    safe_content, findings = _sanitize_for_memory(content, "conversation_memory.content")
    await _require_thread_scope(
        session,
        project_id=project_id,
        chat_id=chat_id,
        user_id=user_id,
        thread_id=thread_id,
    )
    memory_id = uuid4()
    stored_id = (
        await session.execute(
            insert(ConversationMemory)
            .values(
                id=memory_id,
                project_id=project_id,
                thread_id=thread_id,
                kind=kind,
                memory_key=memory_key,
                content=safe_content,
                privacy_findings=findings,
            )
            .on_conflict_do_update(
                constraint="uq_conversation_memory_key",
                set_={
                    "content": safe_content,
                    "privacy_findings": findings,
                    "updated_at": utcnow(),
                },
            )
            .returning(ConversationMemory.id)
        )
    ).scalar_one()
    memory = await session.get(ConversationMemory, stored_id)
    if memory is None:
        raise ServiceError("internal_error", "Conversation memory was not persisted")
    return memory


async def load_conversation_context(
    session: AsyncSession,
    *,
    project_id: UUID,
    chat_id: UUID | None,
    user_id: UUID | None,
    thread_id: UUID,
    message_limit: int = 24,
    fact_limit: int = 50,
    max_chars: int = 24_000,
    exclude_external_id: str | None = None,
    before: datetime | None = None,
) -> ConversationContext:
    if not 1 <= message_limit <= 100 or not 0 <= fact_limit <= 100:
        raise ServiceError("invalid_memory_limit", "Conversation memory limits are invalid")
    if not 3_000 <= max_chars <= 100_000:
        raise ServiceError("invalid_memory_limit", "Conversation context size is invalid")
    await _require_thread_scope(
        session,
        project_id=project_id,
        chat_id=chat_id,
        user_id=user_id,
        thread_id=thread_id,
    )
    summary_query = select(ConversationMemory).where(
        ConversationMemory.project_id == project_id,
        ConversationMemory.thread_id == thread_id,
        ConversationMemory.kind == "summary",
        ConversationMemory.memory_key == "current",
    )
    if before is not None:
        summary_query = summary_query.where(ConversationMemory.updated_at <= before)
    summary = await session.scalar(summary_query)
    fact_query = (
        select(ConversationMemory)
        .where(
            ConversationMemory.project_id == project_id,
            ConversationMemory.thread_id == thread_id,
            ConversationMemory.kind == "fact",
        )
        .order_by(ConversationMemory.updated_at.desc(), ConversationMemory.id.desc())
        .limit(fact_limit)
    )
    if before is not None:
        fact_query = fact_query.where(ConversationMemory.updated_at <= before)
    facts = list(await session.scalars(fact_query))
    message_query = select(ConversationMessage).where(
        ConversationMessage.project_id == project_id,
        ConversationMessage.thread_id == thread_id,
    )
    if exclude_external_id is not None:
        message_query = message_query.where(
            or_(
                ConversationMessage.external_id.is_(None),
                ConversationMessage.external_id != exclude_external_id,
            )
        )
    if before is not None:
        message_query = message_query.where(ConversationMessage.created_at <= before)
    messages = list(
        await session.scalars(
            message_query.order_by(
                ConversationMessage.created_at.desc(), ConversationMessage.id.desc()
            ).limit(message_limit)
        )
    )
    messages.reverse()
    return _bound_context(
        thread_id=thread_id,
        summary=summary,
        facts=facts,
        messages=messages,
        max_chars=max_chars,
    )


async def _require_chat_scope(
    session: AsyncSession,
    *,
    project_id: UUID,
    chat_id: UUID | None,
    user_id: UUID | None,
) -> None:
    if chat_id is None and user_id is None:
        raise ServiceError("conversation_scope_unavailable", "Conversation target is required")
    if chat_id is not None:
        chat = await session.scalar(
            select(TelegramChat.id).where(
                TelegramChat.id == chat_id,
                TelegramChat.project_id == project_id,
                TelegramChat.enabled.is_(True),
            )
        )
        if chat is None:
            raise ServiceError("conversation_scope_unavailable", "Conversation chat is unavailable")
    if user_id is None:
        return
    member = await session.scalar(
        select(ProjectMembership.user_id).where(
            ProjectMembership.project_id == project_id,
            ProjectMembership.user_id == user_id,
        )
    )
    if member is None:
        raise ServiceError("conversation_scope_unavailable", "Conversation user is unavailable")


async def _require_thread_scope(
    session: AsyncSession,
    *,
    project_id: UUID,
    chat_id: UUID | None,
    user_id: UUID | None,
    thread_id: UUID,
) -> ConversationThread:
    thread = await session.scalar(
        select(ConversationThread).where(
            ConversationThread.id == thread_id,
            ConversationThread.project_id == project_id,
            _chat_scope_clause(chat_id),
            _user_scope_clause(user_id),
        )
    )
    if thread is None:
        raise ServiceError("conversation_scope_unavailable", "Conversation is unavailable")
    return thread


async def _find_thread(
    session: AsyncSession,
    *,
    project_id: UUID,
    chat_id: UUID | None,
    user_id: UUID | None,
) -> ConversationThread | None:
    return cast(
        ConversationThread | None,
        await session.scalar(
            select(ConversationThread).where(
                ConversationThread.project_id == project_id,
                _chat_scope_clause(chat_id),
                _user_scope_clause(user_id),
            )
        ),
    )


def _user_scope_clause(user_id: UUID | None) -> ColumnElement[bool]:
    if user_id is None:
        return ConversationThread.user_id.is_(None)
    return ConversationThread.user_id == user_id


def _chat_scope_clause(chat_id: UUID | None) -> ColumnElement[bool]:
    if chat_id is None:
        return ConversationThread.chat_id.is_(None)
    return ConversationThread.chat_id == chat_id


async def _require_message_author(
    session: AsyncSession,
    *,
    project_id: UUID,
    thread_user_id: UUID | None,
    role: str,
    author_user_id: UUID | None,
) -> None:
    if role != "user":
        if author_user_id is not None:
            raise ServiceError("invalid_message_author", "Only user messages have a user author")
        return
    if author_user_id is None or (thread_user_id is not None and author_user_id != thread_user_id):
        raise ServiceError("invalid_message_author", "Message author is outside the conversation")
    member = await session.scalar(
        select(ProjectMembership.user_id).where(
            ProjectMembership.project_id == project_id,
            ProjectMembership.user_id == author_user_id,
        )
    )
    if member is None:
        raise ServiceError("invalid_message_author", "Message author is outside the project")


def _validate_message(
    *,
    role: str,
    source: str,
    content: str,
    external_id: str | None,
) -> None:
    if role not in _MESSAGE_ROLES:
        raise ServiceError("invalid_message", "Conversation role is invalid")
    if _SOURCE_RE.fullmatch(source) is None:
        raise ServiceError("invalid_message", "Conversation source is invalid")
    if not content.strip() or len(content) > 32_000 or "\0" in content:
        raise ServiceError("invalid_message", "Conversation message must contain safe text")
    if external_id is not None and (not 1 <= len(external_id) <= 255 or "\0" in external_id):
        raise ServiceError("invalid_message", "Conversation external id is invalid")


def _validate_memory(*, kind: str, memory_key: str, content: str) -> None:
    if kind not in {"summary", "fact"}:
        raise ServiceError("invalid_memory", "Conversation memory kind is invalid")
    if _MEMORY_KEY_RE.fullmatch(memory_key) is None:
        raise ServiceError("invalid_memory", "Conversation memory key is invalid")
    if kind == "summary" and memory_key != "current":
        raise ServiceError("invalid_memory", "Conversation summary key must be current")
    if not content.strip() or len(content) > 32_000 or "\0" in content:
        raise ServiceError("invalid_memory", "Conversation memory must contain safe text")


def _sanitize_for_memory(content: str, location: str) -> tuple[str, list[PrivacyFinding]]:
    result = sanitize_text(content, level="balanced", location=location)
    return result.text, result.findings


def _ensure_same_message(
    message: ConversationMessage,
    *,
    project_id: UUID,
    role: str,
    author_user_id: UUID | None,
    content: str,
) -> None:
    if (
        message.project_id != project_id
        or message.role != role
        or message.author_user_id != author_user_id
        or message.content != content
    ):
        raise ServiceError(
            "idempotency_conflict",
            "The external message id was already used for different content",
        )


def _bound_context(
    *,
    thread_id: UUID,
    summary: ConversationMemory | None,
    facts: list[ConversationMemory],
    messages: list[ConversationMessage],
    max_chars: int,
) -> ConversationContext:
    summary_text = summary.content[: max_chars // 4] if summary is not None else None
    fact_budget = max_chars // 4
    bounded_facts: list[ConversationFact] = []
    for fact in facts:
        available = fact_budget - len(fact.memory_key) - 2
        if available <= 0:
            break
        content = fact.content[:available]
        bounded_facts.append(ConversationFact(key=fact.memory_key, content=content))
        fact_budget -= len(fact.memory_key) + len(content) + 2

    used = len(summary_text or "") + sum(
        len(fact.key) + len(fact.content) + 2 for fact in bounded_facts
    )
    message_budget = max_chars - used
    bounded_messages: list[ConversationContextMessage] = []
    for message in reversed(messages):
        if message_budget <= 0:
            break
        content = message.content[:message_budget]
        bounded_messages.append(
            ConversationContextMessage(
                role=message.role,
                source=message.source,
                content=content,
                author_user_id=message.author_user_id,
                created_at=message.created_at,
            )
        )
        message_budget -= len(content)
    bounded_messages.reverse()
    return ConversationContext(
        thread_id=thread_id,
        summary=summary_text,
        facts=tuple(bounded_facts),
        messages=tuple(bounded_messages),
    )
