from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from dca.domain import (
    ChangeRequestStatus,
    Citation,
    ClarificationStatus,
    InvalidStateTransition,
    ensure_transition,
    parse_service_token,
    validate_citation,
)


def test_clarification_state_machine_is_terminal_after_answer() -> None:
    ensure_transition(ClarificationStatus.PENDING, ClarificationStatus.ANSWERED)
    with pytest.raises(InvalidStateTransition):
        ensure_transition(ClarificationStatus.ANSWERED, ClarificationStatus.CANCELLED)


def test_change_request_cannot_skip_in_progress() -> None:
    with pytest.raises(InvalidStateTransition):
        ensure_transition(ChangeRequestStatus.OPEN, ChangeRequestStatus.DONE)


@pytest.mark.parametrize(
    "path",
    ["../secret.env", "/etc/passwd", "src/../../secret", "C:\\Windows\\secret"],
)
def test_citation_rejects_path_traversal(path: str) -> None:
    with pytest.raises(ValidationError):
        Citation(path=path, start_line=1, end_line=1)


def test_citation_validates_exact_file_range(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "api.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    accepted = validate_citation(
        tmp_path,
        Citation(path="src/api.py", start_line=2, end_line=3),
    )
    rejected = validate_citation(
        tmp_path,
        Citation(path="src/api.py", start_line=2, end_line=4),
    )
    assert accepted.accepted is True
    assert rejected.accepted is False
    assert rejected.reason == "line_out_of_range"


def test_service_token_parser_requires_prefix_and_entropy() -> None:
    parsed = parse_service_token("dca_deadbeef_abcdefghijklmnopqrstuvwxyz012345")
    assert parsed is not None
    assert parsed.prefix == "deadbeef"
    assert parse_service_token("plain-secret") is None
