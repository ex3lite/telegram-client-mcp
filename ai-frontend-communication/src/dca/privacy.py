from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, TypedDict

PrivacyLevel = Literal["balanced", "strict"]


class PrivacyFinding(TypedDict):
    kind: str
    location: str
    action: Literal["blocked", "redacted"]


@dataclass(frozen=True, slots=True)
class PrivacyResult:
    text: str
    findings: list[PrivacyFinding]
    blocked: bool


_SECRET_PATTERNS = (
    (
        "private_key",
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?"
        r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
    ),
    (
        "credential_url",
        r"\b[a-z][a-z0-9+.-]{1,20}://[^\s/@:]+:[^\s/@]+@[^\s<>\"']+",
    ),
    ("bearer_token", r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    ("telegram_token", r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    ("dca_token", r"\bdca_[0-9a-f]{8}_[A-Za-z0-9_-]{20,}\b"),
    ("github_token", r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    ("slack_token", r"\b(?:xox[baprs]-[A-Za-z0-9-]{20,}|xapp-[A-Za-z0-9-]{20,})\b"),
    ("anthropic_token", r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    ("openai_token", r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b"),
    ("aws_access_key", r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    (
        "secret_assignment",
        r"\b\"?(?:(?:aws[_-]?)?access[_-]?key|api[_-]?key|secret(?:[_-]?key)?|"
        r"access[_-]?token|refresh[_-]?token|"
        r"auth(?:orization)?|password|passwd|pwd|client[_-]?secret|private[_-]?key|"
        r"credential)\"?\s*[:=]\s*(?:\"[^\"\r\n]{4,}\"|'[^'\r\n]{4,}'|[^\s,;}\]]{8,})",
    ),
)
_SECRET_RE = re.compile(
    "|".join(f"(?P<p{index}>{pattern})" for index, (_, pattern) in enumerate(_SECRET_PATTERNS)),
    re.IGNORECASE | re.MULTILINE,
)


def sanitize_text(text: str, *, level: PrivacyLevel, location: str) -> PrivacyResult:
    """Detect secrets without retaining their values; strict callers must fail closed."""
    matches = list(_SECRET_RE.finditer(text))
    if not matches:
        return PrivacyResult(text=text, findings=[], blocked=False)

    action: Literal["blocked", "redacted"] = "blocked" if level == "strict" else "redacted"
    findings: list[PrivacyFinding] = []
    chunks: list[str] = []
    cursor = 0
    for match in matches:
        kind_index = next(index for index, value in enumerate(match.groups()) if value is not None)
        kind = _SECRET_PATTERNS[kind_index][0]
        chunks.extend((text[cursor : match.start()], f"[REDACTED:{kind}]"))
        cursor = match.end()
        findings.append({"kind": kind, "location": location, "action": action})
    chunks.append(text[cursor:])
    return PrivacyResult(text="".join(chunks), findings=findings, blocked=level == "strict")
