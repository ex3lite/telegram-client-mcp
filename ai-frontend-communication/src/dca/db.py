from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from dca.config import Settings
from dca.domain import (
    ChangeRequestStatus,
    ClarificationStatus,
    JobStatus,
    RepositoryStatus,
)


class Base(DeclarativeBase):
    pass


def uuid_column(*, primary_key: bool = False) -> Mapped[UUID]:
    return mapped_column(PGUUID(as_uuid=True), primary_key=primary_key, default=uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Project(Base, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[UUID] = uuid_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ProjectAgentSettings(Base, TimestampMixin):
    __tablename__ = "project_agent_settings"

    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    claude_model: Mapped[str | None] = mapped_column(String(120))
    claude_effort: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    claude_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=1_200)
    max_budget_cents: Mapped[int | None] = mapped_column(Integer)
    base_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    answer_style: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")
    privacy_level: Mapped[str] = mapped_column(String(16), nullable=False, default="strict")
    denied_globs: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    memory_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    memory_recent_messages: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    memory_max_context_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=500_000)
    telegram_group_mode: Mapped[str] = mapped_column(String(24), nullable=False, default="mentions")
    telegram_private_mode: Mapped[str] = mapped_column(
        String(24), nullable=False, default="all_messages"
    )
    telegram_streaming_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    telegram_attach_markdown: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_by_admin_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("admin_principals.id", ondelete="SET NULL"),
    )


class Repository(Base, TimestampMixin):
    __tablename__ = "repositories"
    __table_args__ = (
        UniqueConstraint("project_id", "name"),
        UniqueConstraint(
            "project_id",
            "github_repository",
            name="uq_repositories_project_github_repository",
        ),
        CheckConstraint(
            "NOT auto_sync_enabled OR github_repository IS NOT NULL",
            name="ck_repository_auto_sync_source",
        ),
        CheckConstraint(
            "sync_generation >= 0",
            name="ck_repository_sync_generation",
        ),
    )

    id: Mapped[UUID] = uuid_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    ssh_url: Mapped[str] = mapped_column(String(1_024), nullable=False)
    default_branch: Mapped[str] = mapped_column(String(255), nullable=False, default="main")
    allowed_paths: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    deploy_key_path: Mapped[str | None] = mapped_column(String(1_024))
    known_hosts_path: Mapped[str | None] = mapped_column(String(1_024))
    mirror_path: Mapped[str | None] = mapped_column(String(1_024))
    github_repository: Mapped[str | None] = mapped_column(String(255))
    auto_sync_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    sync_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    current_commit: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=RepositoryStatus.NEVER_SYNCED.value
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_webhook_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_webhook_commit: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[UUID] = uuid_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class AdminPrincipal(Base, TimestampMixin):
    __tablename__ = "admin_principals"

    id: Mapped[UUID] = uuid_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class AdminAccessKey(Base, TimestampMixin):
    __tablename__ = "admin_access_keys"

    id: Mapped[UUID] = uuid_column(primary_key=True)
    principal_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("admin_principals.id", ondelete="CASCADE"),
        nullable=False,
    )
    fingerprint: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AdminSession(Base, TimestampMixin):
    __tablename__ = "admin_sessions"
    __table_args__ = (Index("ix_admin_session_access_key", "access_key_id"),)

    id: Mapped[UUID] = uuid_column(primary_key=True)
    access_key_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("admin_access_keys.id", ondelete="CASCADE"),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProjectMembership(Base):
    __tablename__ = "project_memberships"
    __table_args__ = (
        CheckConstraint(
            "knowledge_scope IN ('integration', 'internal')",
            name="ck_project_membership_knowledge_scope",
        ),
    )

    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[str] = mapped_column(String(40), nullable=False, default="developer")
    department: Mapped[str | None] = mapped_column(String(80))
    stack: Mapped[str | None] = mapped_column(String(160))
    preferred_language: Mapped[str] = mapped_column(String(16), nullable=False, default="ru")
    knowledge_scope: Mapped[str] = mapped_column(String(24), nullable=False, default="integration")
    can_create_requests: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class TelegramIdentity(Base, TimestampMixin):
    __tablename__ = "telegram_identities"

    id: Mapped[UUID] = uuid_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64))
    private_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reachable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class TelegramChat(Base, TimestampMixin):
    __tablename__ = "telegram_chats"
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_telegram_chat_id_project"),
        UniqueConstraint(
            "telegram_chat_id",
            "message_thread_id",
            name="uq_chat_thread",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[UUID] = uuid_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_thread_id: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ConversationThread(Base, TimestampMixin):
    __tablename__ = "conversation_threads"
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_conversation_thread_id_project"),
        UniqueConstraint(
            "project_id",
            "chat_id",
            "user_id",
            name="uq_conversation_thread_scope",
            postgresql_nulls_not_distinct=True,
        ),
        ForeignKeyConstraint(
            ["chat_id", "project_id"],
            ["telegram_chats.id", "telegram_chats.project_id"],
            name="fk_conversation_thread_chat_project",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "user_id"],
            ["project_memberships.project_id", "project_memberships.user_id"],
            name="fk_conversation_thread_member",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "chat_id IS NOT NULL OR user_id IS NOT NULL",
            name="ck_conversation_thread_target",
        ),
        CheckConstraint(
            "claude_compaction_count >= 0",
            name="ck_conversation_thread_compaction_count",
        ),
        Index("ix_conversation_thread_project_recent", "project_id", "last_message_at"),
    )

    id: Mapped[UUID] = uuid_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    chat_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claude_session_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    claude_repository_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("repositories.id", ondelete="SET NULL")
    )
    claude_commit_sha: Mapped[str | None] = mapped_column(String(64))
    claude_policy_hash: Mapped[str | None] = mapped_column(CHAR(64))
    claude_compaction_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    claude_last_compacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claude_context_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConversationMessage(Base, TimestampMixin):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        UniqueConstraint(
            "thread_id",
            "source",
            "external_id",
            name="uq_conversation_message_source",
        ),
        ForeignKeyConstraint(
            ["thread_id", "project_id"],
            ["conversation_threads.id", "conversation_threads.project_id"],
            name="fk_conversation_message_thread_project",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "role IN ('user', 'assistant', 'agent', 'tool')",
            name="ck_conversation_message_role",
        ),
        CheckConstraint(
            "source ~ '^[a-z][a-z0-9_.-]{1,31}$'",
            name="ck_conversation_message_source",
        ),
        CheckConstraint(
            "char_length(content) BETWEEN 1 AND 32000",
            name="ck_conversation_message_content",
        ),
        CheckConstraint(
            "jsonb_typeof(privacy_findings) = 'array'",
            name="ck_conversation_message_privacy",
        ),
        Index(
            "ix_conversation_message_thread_recent",
            "project_id",
            "thread_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = uuid_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    thread_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(255))
    author_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    privacy_findings: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )


class ConversationMemory(Base, TimestampMixin):
    __tablename__ = "conversation_memories"
    __table_args__ = (
        UniqueConstraint(
            "thread_id",
            "kind",
            "memory_key",
            name="uq_conversation_memory_key",
        ),
        ForeignKeyConstraint(
            ["thread_id", "project_id"],
            ["conversation_threads.id", "conversation_threads.project_id"],
            name="fk_conversation_memory_thread_project",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "kind IN ('summary', 'fact')",
            name="ck_conversation_memory_kind",
        ),
        CheckConstraint(
            "memory_key ~ '^[a-z0-9][a-z0-9_.:-]{0,127}$'",
            name="ck_conversation_memory_key",
        ),
        CheckConstraint(
            "char_length(content) BETWEEN 1 AND 32000",
            name="ck_conversation_memory_content",
        ),
        CheckConstraint(
            "jsonb_typeof(privacy_findings) = 'array'",
            name="ck_conversation_memory_privacy",
        ),
        Index("ix_conversation_memory_thread_kind", "project_id", "thread_id", "kind"),
    )

    id: Mapped[UUID] = uuid_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    thread_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    memory_key: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    privacy_findings: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )


class ServiceAccount(Base, TimestampMixin):
    __tablename__ = "service_accounts"

    id: Mapped[UUID] = uuid_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(8), unique=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    tool_scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class ServiceAccountProject(Base):
    __tablename__ = "service_account_projects"

    service_account_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("service_accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )


class TelegramUpdate(Base):
    __tablename__ = "telegram_updates"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    update_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Interaction(Base, TimestampMixin):
    __tablename__ = "interactions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_thread_id", "project_id"],
            ["conversation_threads.id", "conversation_threads.project_id"],
            name="fk_interaction_conversation_project",
            ondelete="SET NULL (conversation_thread_id)",
        ),
    )

    id: Mapped[UUID] = uuid_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    repository_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("repositories.id", ondelete="SET NULL")
    )
    conversation_thread_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    correlation_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    answer_markdown: Mapped[str | None] = mapped_column(Text)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    rejected_citations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    uncertainty: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    provider_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64))
    artifacts: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    privacy_findings: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )


class AgentMessage(Base, TimestampMixin):
    __tablename__ = "agent_messages"
    __table_args__ = (
        UniqueConstraint(
            "service_account_id",
            "idempotency_key",
            name="uq_agent_message_idempotency",
        ),
        Index("ix_agent_message_project_status", "project_id", "status"),
    )

    id: Mapped[UUID] = uuid_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    service_account_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("service_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    correlation_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    target_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    target_chat_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("telegram_chats.id", ondelete="RESTRICT")
    )
    text_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    attachment_name: Mapped[str | None] = mapped_column(String(255))
    attachment_markdown: Mapped[str | None] = mapped_column(Text)
    privacy_findings: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    error_code: Mapped[str | None] = mapped_column(String(64))


class Clarification(Base, TimestampMixin):
    __tablename__ = "clarifications"
    __table_args__ = (
        UniqueConstraint(
            "service_account_id",
            "idempotency_key",
            name="uq_clarification_idempotency",
        ),
        Index("ix_clarification_pending_expiry", "status", "expires_at"),
    )

    id: Mapped[UUID] = uuid_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    service_account_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("service_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    recipient_user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    agent_run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    context: Mapped[str] = mapped_column(Text, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    expected_answer: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ClarificationStatus.PENDING.value
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    answer_raw: Mapped[str | None] = mapped_column(Text)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_reason: Mapped[str | None] = mapped_column(Text)


class ChangeRequest(Base, TimestampMixin):
    __tablename__ = "change_requests"
    __table_args__ = (
        Index("ix_change_request_project_status", "project_id", "status"),
        UniqueConstraint(
            "source_interaction_id",
            name="uq_change_request_source_interaction",
        ),
        CheckConstraint(
            "jsonb_typeof(requester_profile) = 'object'",
            name="ck_change_request_requester_profile",
        ),
        CheckConstraint(
            "jsonb_typeof(citations) = 'array'",
            name="ck_change_request_citations",
        ),
    )

    id: Mapped[UUID] = uuid_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    source_interaction_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("interactions.id", ondelete="SET NULL")
    )
    correlation_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    requester_profile: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    question: Mapped[str] = mapped_column(Text, nullable=False, default="")
    agent_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ChangeRequestStatus.OPEN.value
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class Job(Base, TimestampMixin):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_job_claim", "status", "available_at", "created_at"),
        UniqueConstraint("deduplication_key", name="uq_job_deduplication_key"),
    )

    id: Mapped[UUID] = uuid_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=JobStatus.QUEUED.value)
    deduplication_key: Mapped[str | None] = mapped_column(String(255))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(255))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_detail: Mapped[str | None] = mapped_column(Text)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_correlation_time", "correlation_id", "occurred_at"),
        Index("ix_audit_project_time", "project_id", "occurred_at"),
    )

    id: Mapped[UUID] = uuid_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    event_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    project_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL")
    )
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    causation_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    subject_type: Mapped[str | None] = mapped_column(String(64))
    subject_id: Mapped[str | None] = mapped_column(String(255))
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, default="success")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    remote_address_hash: Mapped[bytes | None] = mapped_column(LargeBinary)


class SystemSecret(Base, TimestampMixin):
    __tablename__ = "system_secrets"

    name: Mapped[str] = mapped_column(String(120), primary_key=True)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    updated_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("admin_principals.id", ondelete="SET NULL"),
    )


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )


class Database:
    def __init__(self, settings: Settings) -> None:
        self.engine = create_engine(settings)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def close(self) -> None:
        await self.engine.dispose()


async def append_audit(
    session: AsyncSession,
    *,
    event_type: str,
    correlation_id: str,
    actor_type: str,
    actor_id: str,
    project_id: UUID | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    outcome: str = "success",
    payload: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        event_type=event_type,
        correlation_id=correlation_id,
        actor_type=actor_type,
        actor_id=actor_id,
        project_id=project_id,
        subject_type=subject_type,
        subject_id=subject_id,
        outcome=outcome,
        payload=payload or {},
    )
    session.add(event)
    await session.flush()
    return event


async def enqueue_job(
    session: AsyncSession,
    *,
    kind: str,
    payload: dict[str, Any],
    deduplication_key: str | None = None,
    max_attempts: int = 5,
) -> Job:
    job = Job(
        kind=kind,
        payload=payload,
        deduplication_key=deduplication_key,
        max_attempts=max_attempts,
    )
    session.add(job)
    await session.flush()
    return job


async def enqueue_repository_sync(
    session: AsyncSession,
    *,
    repository: Repository,
    source: str,
    deduplication_key: str,
    requested_commit: str | None = None,
) -> tuple[Job, bool]:
    """Queue one causally ordered sync, serializing generation allocation in PostgreSQL."""
    locked_repository = await session.scalar(
        select(Repository).where(Repository.id == repository.id).with_for_update()
    )
    if locked_repository is None:
        raise RuntimeError("Repository disappeared while queueing synchronization")
    repository = locked_repository
    existing = await session.scalar(select(Job).where(Job.deduplication_key == deduplication_key))
    if existing is not None:
        return existing, False
    repository.sync_generation += 1
    job = await enqueue_job(
        session,
        kind="repository.sync",
        payload={
            "repository_id": str(repository.id),
            "generation": repository.sync_generation,
            "requested_commit": requested_commit,
            "source": source,
        },
        deduplication_key=deduplication_key,
        max_attempts=3,
    )
    return job, True
