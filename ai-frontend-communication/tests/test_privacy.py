from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from dca.domain import AskUserInput, KnowledgeAnswer, KnowledgeArtifact, utcnow
from dca.privacy import sanitize_text
from dca.service import ServiceError, sanitize_clarification_request
from dca.worker import sanitize_knowledge_answer


@pytest.mark.parametrize(
    "expected_kind, value",
    [
        (
            "private_key",
            "-----BEGIN PRIVATE KEY-----\nabc123\n-----END PRIVATE KEY-----",
        ),
        ("credential_url", "postgresql://user:password@example.test/db"),
        ("bearer_token", "Bearer abcdefghijklmnopqrstuvwxyz"),
        ("telegram_token", "1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"),
        ("dca_token", "dca_deadbeef_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"),
        ("github_token", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"),
        (
            "slack_token",
            "".join(("xox", "b-123456789012-", "123456789012-", "abcdefghijklmnopqrstuvwx")),
        ),
        ("anthropic_token", "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"),
        ("openai_token", "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"),
        ("aws_access_key", "AKIA1234567890ABCDEF"),
        ("secret_assignment", "PASSWORD=abcdefghijklmnopqrstuvwxyz"),
    ],
)
def test_required_secret_patterns_are_detected(expected_kind: str, value: str) -> None:
    result = sanitize_text(value, level="strict", location="test")

    assert result.blocked is True
    assert result.findings[0]["kind"] == expected_kind
    assert value not in result.text


def test_balanced_redacts_without_retaining_secret_value() -> None:
    secret = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz123456"  # noqa: S105
    result = sanitize_text(
        f"Use Authorization: Bearer {secret}",
        level="balanced",
        location="answer_markdown",
    )

    assert secret not in result.text
    assert result.findings == [
        {"kind": "bearer_token", "location": "answer_markdown", "action": "redacted"}
    ]
    assert secret not in repr(result.findings)
    assert result.blocked is False


def test_clarification_privacy_covers_nested_expected_answer() -> None:
    secret = "Bearer abcdefghijklmnopqrstuvwxyz"  # noqa: S105
    request = AskUserInput(
        project_id=uuid4(),
        agent_run_id="run-1",
        correlation_id="corr-1",
        idempotency_key="privacy-clarification-1",
        recipient_user_id=uuid4(),
        context="Safe context",
        question="Can you check this?",
        expected_answer={"example": {"authorization": secret}},
        expires_at=utcnow() + timedelta(hours=1),
    )

    with pytest.raises(ServiceError) as blocked:
        sanitize_clarification_request(request, level="strict")
    assert blocked.value.code == "privacy_blocked"
    assert secret not in str(blocked.value.metadata)

    sanitized, findings = sanitize_clarification_request(request, level="balanced")
    assert sanitized.expected_answer == {"example": {"authorization": "[REDACTED:bearer_token]"}}
    assert findings[0]["location"] == "clarification.expected_answer.example.authorization"


def test_strict_blocks_secret_in_markdown_artifact() -> None:
    answer = KnowledgeAnswer(
        answer_markdown="Safe summary",
        context_attestation={
            "contract_version": "dca-context-v1",
            "nonce": "1" * 32,
            "policy_sha256": "2" * 64,
            "context_sha256": "3" * 64,
        },
        artifacts=[
            KnowledgeArtifact(name="runbook.md", content="AWS_ACCESS_KEY=AKIA1234567890ABCDEF")
        ],
    )

    sanitized, findings, blocked = sanitize_knowledge_answer(answer, level="strict")

    assert blocked is True
    assert sanitized.artifacts[0].content == "[REDACTED:secret_assignment]"
    assert findings == [
        {"kind": "secret_assignment", "location": "artifact:runbook.md", "action": "blocked"}
    ]


@pytest.mark.parametrize("name", ["../report.md", "report.txt", ".hidden.md", "a/notes.md"])
def test_markdown_artifact_name_stays_a_safe_basename(name: str) -> None:
    with pytest.raises(ValidationError):
        KnowledgeArtifact(name=name, content="safe")
