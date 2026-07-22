from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class ClarificationStatus(StrEnum):
    PENDING = "pending"
    ANSWERED = "answered"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ChangeRequestStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    REJECTED = "rejected"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY = "retry"
    SUCCEEDED = "succeeded"
    DELIVERY_UNCERTAIN = "delivery_uncertain"
    FAILED = "failed"


class RepositoryStatus(StrEnum):
    NEVER_SYNCED = "never_synced"
    SYNCING = "syncing"
    READY = "ready"
    STALE = "stale"
    FAILED = "failed"
    DISABLED = "disabled"


ALLOWED_CLARIFICATION_TRANSITIONS: dict[ClarificationStatus, set[ClarificationStatus]] = {
    ClarificationStatus.PENDING: {
        ClarificationStatus.ANSWERED,
        ClarificationStatus.EXPIRED,
        ClarificationStatus.CANCELLED,
    },
    ClarificationStatus.ANSWERED: set(),
    ClarificationStatus.EXPIRED: set(),
    ClarificationStatus.CANCELLED: set(),
}

ALLOWED_CHANGE_REQUEST_TRANSITIONS: dict[ChangeRequestStatus, set[ChangeRequestStatus]] = {
    ChangeRequestStatus.OPEN: {
        ChangeRequestStatus.IN_PROGRESS,
        ChangeRequestStatus.REJECTED,
    },
    ChangeRequestStatus.IN_PROGRESS: {
        ChangeRequestStatus.DONE,
        ChangeRequestStatus.REJECTED,
    },
    ChangeRequestStatus.DONE: set(),
    ChangeRequestStatus.REJECTED: set(),
}


class InvalidStateTransition(ValueError):
    pass


def ensure_transition(current: StrEnum, target: StrEnum) -> None:
    if isinstance(current, ClarificationStatus) and isinstance(target, ClarificationStatus):
        allowed: set[StrEnum] = set(ALLOWED_CLARIFICATION_TRANSITIONS[current])
    elif isinstance(current, ChangeRequestStatus) and isinstance(target, ChangeRequestStatus):
        allowed = set(ALLOWED_CHANGE_REQUEST_TRANSITIONS[current])
    else:
        raise InvalidStateTransition(f"incompatible states: {current} -> {target}")
    if target not in allowed:
        raise InvalidStateTransition(f"invalid transition: {current} -> {target}")


class Citation(BaseModel):
    path: str = Field(min_length=1, max_length=1_024)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @field_validator("path")
    @classmethod
    def normalize_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/").strip()
        candidate = PurePosixPath(normalized)
        if (
            candidate.is_absolute()
            or re.match(r"^[A-Za-z]:/", normalized)
            or ".." in candidate.parts
            or not candidate.parts
        ):
            raise ValueError("citation path must stay inside the repository")
        return candidate.as_posix()

    @model_validator(mode="after")
    def line_range_is_ordered(self) -> Citation:
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class CitationCheck(BaseModel):
    citation: Citation
    accepted: bool
    reason: str | None = None


def validate_citation(snapshot_root: Path, citation: Citation) -> CitationCheck:
    root = snapshot_root.resolve()
    candidate = (root / citation.path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return CitationCheck(citation=citation, accepted=False, reason="path_outside_snapshot")
    if not candidate.is_file():
        return CitationCheck(citation=citation, accepted=False, reason="source_not_found")
    try:
        with candidate.open("r", encoding="utf-8", errors="replace") as source:
            line_count = sum(1 for _ in source)
    except OSError:
        return CitationCheck(citation=citation, accepted=False, reason="source_unreadable")
    if citation.end_line > line_count:
        return CitationCheck(citation=citation, accepted=False, reason="line_out_of_range")
    return CitationCheck(citation=citation, accepted=True)


class KnowledgeArtifact(BaseModel):
    name: str = Field(min_length=4, max_length=128)
    content: str = Field(min_length=1, max_length=500_000)

    @field_validator("name")
    @classmethod
    def require_safe_markdown_name(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.md", value) or ".." in value:
            raise ValueError("artifact name must be a safe .md filename")
        return value


class AgentContextAttestation(BaseModel):
    contract_version: Literal["dca-context-v1"]
    nonce: str = Field(pattern=r"^[a-f0-9]{32}$")
    policy_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    context_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class AgentChangeRequestProposal(BaseModel):
    """The only ticket fields Claude may control."""

    model_config = {"extra": "forbid"}

    kind: Literal["bug", "feature", "integration", "change", "question"]
    title: str = Field(min_length=3, max_length=200)
    summary: str = Field(min_length=1, max_length=16_000)
    priority: Literal["low", "normal", "high", "urgent"] = "normal"

    @field_validator("title", "summary")
    @classmethod
    def normalize_agent_request_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("agent request text cannot be blank")
        return normalized


_AGENT_REQUEST_INFO_RE = re.compile(
    r"^(?:(?:@?[\w.-]+)[,:!]\s*)?"
    r"(?:как\b|почему\b|зачем\b|что\s+такое\b|можно\s+ли\b|"
    r"how(?:\s+to)?\b|why\b|what\s+(?:is|are)\b|can\s+(?:i|we)\b)"
)
_AGENT_TICKET_REQUEST_RE = re.compile(
    r"\b(?:созда(?:й|йте|ть)|завед(?:и|ите|сти)|оформ(?:и|ите|ить))\b.{0,80}"
    r"\b(?:заявк\w*|задач\w*|тикет\w*|issue|request)\b|"
    r"\b(?:переда(?:й|йте|ть)|отправ(?:ь|ьте|ить)|эскалиру(?:й|йте|овать))\b.{0,80}"
    r"\b(?:backend|back-end|б[еэ]кенд\w*|бэкэнд\w*)\b|"
    r"\b(?:create|file|open)\b.{0,40}\b(?:ticket|issue|request)\b|"
    r"\b(?:send|escalate)\b.{0,40}\b(?:backend|back-end|backend\s+team)\b"
)
_AGENT_BACKEND_CHANGE_RE = re.compile(
    r"\b(?:добав(?:ь|ьте)|реализу(?:й|йте)|почин(?:и|ите)|исправ(?:ь|ьте)|"
    r"доработа(?:й|йте)|измен(?:и|ите)|внедр(?:и|ите)|обнов(?:и|ите))\b.{0,100}"
    r"\b(?:api|апи|backend|back-end|б[еэ]кенд\w*|бэкэнд\w*|сервер\w*|"
    r"endpoint|эндпоинт\w*|webhook|вебхук\w*|контракт\w*|фич\w*|feature|bug|баг\w*)\b|"
    r"\b(?:нужн\w*|надо|необходим\w*|требуется|прошу|мож(?:ешь|ете))\b.{0,60}"
    r"\b(?:добавить|реализовать|починить|исправить|доработать|изменить|внедрить|обновить|"
    r"доработк\w*|изменени\w*|исправлени\w*)\b.{0,100}"
    r"\b(?:api|апи|backend|back-end|б[еэ]кенд\w*|бэкэнд\w*|сервер\w*|"
    r"endpoint|эндпоинт\w*|webhook|вебхук\w*|контракт\w*|фич\w*|feature|bug|баг\w*)\b|"
    r"\b(?:please\s+)?(?:add|implement|fix|change|update|extend|build)\b.{0,100}"
    r"\b(?:api|backend|back-end|server|endpoint|webhook|contract|feature|bug)\b|"
    r"\b(?:we\s+need|need|please|can\s+you|could\s+you)\b.{0,60}"
    r"\b(?:add|implement|fix|change|update|extend|build)\b.{0,100}"
    r"\b(?:api|backend|back-end|server|endpoint|webhook|contract|feature|bug)\b|"
    r"\b(?:нужн\w*|необходим\w*|требуется)\b.{0,80}"
    r"\bнов\w+\s+(?:endpoint|эндпоинт\w*|api|апи|backend|б[еэ]кенд\w*)\b|"
    r"\bneed\b.{0,80}\b(?:api|backend|server|endpoint|webhook|feature)\s+"
    r"(?:change|fix|addition|update)\b"
)


def has_explicit_backend_request_intent(question: str) -> bool:
    """Accept only an explicit user request to hand work to the backend team."""
    normalized = " ".join(question.casefold().replace("ё", "е").split())
    if not normalized:
        return False
    if _AGENT_TICKET_REQUEST_RE.search(normalized):
        return True
    if _AGENT_REQUEST_INFO_RE.search(normalized):
        return False
    return _AGENT_BACKEND_CHANGE_RE.search(normalized) is not None


class KnowledgeAnswer(BaseModel):
    answer_markdown: str = Field(min_length=1, max_length=200_000)
    citations: list[Citation] = Field(default_factory=list, max_length=100)
    uncertainty: list[str] = Field(default_factory=list, max_length=50)
    artifacts: list[KnowledgeArtifact] = Field(default_factory=list, max_length=8)
    memory_summary: str | None = Field(default=None, max_length=16_000)
    context_attestation: AgentContextAttestation
    change_request: AgentChangeRequestProposal | None = None

    @model_validator(mode="after")
    def artifact_names_are_unique(self) -> KnowledgeAnswer:
        names = [artifact.name.casefold() for artifact in self.artifacts]
        if len(names) != len(set(names)):
            raise ValueError("artifact names must be unique")
        if sum(len(artifact.content.encode()) for artifact in self.artifacts) > 1_000_000:
            raise ValueError("artifact content exceeds the combined size limit")
        return self


class AskUserInput(BaseModel):
    project_id: UUID
    agent_run_id: str = Field(min_length=1, max_length=255)
    correlation_id: str = Field(min_length=1, max_length=255)
    idempotency_key: str = Field(min_length=8, max_length=255)
    recipient_user_id: UUID
    context: str = Field(min_length=1, max_length=8_000)
    question: str = Field(min_length=1, max_length=4_000)
    expected_answer: dict[str, Any] | None = None
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def expiry_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("expires_at must include a timezone")
        return value.astimezone(UTC)


class ClarificationResult(BaseModel):
    request_id: UUID
    status: ClarificationStatus
    answer: str | None = None
    answered_at: datetime | None = None
    expires_at: datetime


class ChangeRequestCreate(BaseModel):
    project_id: UUID
    kind: str = Field(
        default="task",
        pattern=r"^(bug|task|feature|integration|change|question)$",
    )
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(default="", max_length=16_000)
    priority: str = Field(default="normal", pattern=r"^(low|normal|high|urgent)$")


@dataclass(frozen=True, slots=True)
class ServiceCredential:
    prefix: str
    secret: str


SERVICE_TOKEN_RE = re.compile(r"^dca_([a-z0-9]{8})_([A-Za-z0-9_-]{32,})$")


def parse_service_token(token: str) -> ServiceCredential | None:
    match = SERVICE_TOKEN_RE.fullmatch(token)
    if not match:
        return None
    return ServiceCredential(prefix=match.group(1), secret=match.group(2))


def utcnow() -> datetime:
    return datetime.now(UTC)
