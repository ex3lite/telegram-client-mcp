from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, TypedDict

PrivacyLevel = Literal["balanced", "strict"]
SECURITY_GUARD_ROLE: Literal["bydlo_guard"] = "bydlo_guard"


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

_SECRET_REQUEST_TARGET_PATTERNS = (
    (
        "private_key",
        r"(?:private|приватн(?:ый|ого|ому|ым|ом|ые|ых))\s+"
        r"(?:key|ключ(?:а|у|ом|е|и|ей|ами|ах)?)|"
        r"(?:ssh[- ]?key|ssh[- ]?ключ(?:а|у|ом|е|и|ей|ами|ах)?|id_rsa|id_ed25519)",
    ),
    (
        "api_key",
        r"(?:api[- _]?(?:key|ключ(?:а|у|ом|е|и|ей|ами|ах)?)|"
        r"(?:key|ключ(?:а|у|ом|е|и|ей|ами|ах)?)\s+(?:api|апи))",
    ),
    (
        "token",
        r"(?:access|refresh|bearer|auth|oauth|telegram|bot|доступа|бота)?[- _]?"
        r"(?:token|токен(?:а|у|ом|е|ы|ов|ам|ами|ах)?)",
    ),
    ("password", r"(?:password|passwd|парол(?:ь|я|ю|ем|е|и|ей|ям|ями|ях))"),
    ("credentials", r"(?:credentials?|credential|креды|кредов|учетные\s+данные)"),
    ("secret", r"(?:secrets?|секрет(?:а|у|ом|е|ы|ов|ам|ами|ах)?)"),
    (
        "environment_file",
        r"(?<![\w.])\.env(?:\.[a-z0-9_-]+)?(?![\w.])|"
        r"(?:env|environment)[- _]?(?:file|файл)",
    ),
    (
        "auth_material",
        r"(?:authorization[- _]?header|auth[- _]?header|заголовок\s+authorization|"
        r"session[- _]?cookie|куки\s+сессии)",
    ),
)
_SECRET_REQUEST_TARGET = (
    "(?:" + "|".join(pattern for _, pattern in _SECRET_REQUEST_TARGET_PATTERNS) + ")"
)
_SECRET_REQUEST_ACTION = (
    r"(?:дай|дайте|скинь|скиньте|пришли|пришлите|слей|слейте|раскрой|раскройте|"  # noqa: S105 - regex vocabulary
    r"выведи|выведите|напечатай|напечатайте|покажи|покажите|верни|верните|"
    r"прочитай|прочитайте|достань|достаньте|скопируй|скопируйте|"
    r"give|send|dump|reveal|print|show|output|extract|read|leak|copy)"
)
_SECRET_REQUEST_QUALIFIER = (
    r"(?:мне|нам|сюда|все|весь|всю|всех|сам|само|саму|реальный|реальную|"  # noqa: S105 - regex vocabulary
    r"текущий|текущую|боевой|боевую|продовый|продовую|production|actual|"
    r"prod|current|all|the|our|your|значение|содержимое)"
)
_DIRECT_SECRET_REQUEST_RE = re.compile(
    rf"\b{_SECRET_REQUEST_ACTION}\b(?:\s+{_SECRET_REQUEST_QUALIFIER})*\s+"
    rf"{_SECRET_REQUEST_TARGET}",
    re.IGNORECASE,
)
_SECRET_VALUE_REQUEST_RE = re.compile(
    rf"(?:\b(?:значение|содержимое|value|contents?)\b.{{0,40}}{_SECRET_REQUEST_TARGET}|"
    rf"{_SECRET_REQUEST_TARGET}.{{0,40}}\b(?:целиком|полностью|значение|value)\b)",
    re.IGNORECASE,
)
_SECRET_FILE_LINE_REQUEST_RE = re.compile(
    rf"\b(?:первая|первую|последняя|последнюю|вторая|вторую|first|last|second)\s+"
    rf"(?:строка|строку|line)\b.{{0,60}}{_SECRET_REQUEST_TARGET}",
    re.IGNORECASE,
)
_OWN_SECRET_REQUEST_RE = re.compile(
    rf"\b(?:какой|какая|какое|what\s+is)\b\s+"
    rf"(?:(?:у\s+нас|наш|наша|текущий|боевой|продовый|our|current|production|prod)\s+)+"
    rf"{_SECRET_REQUEST_TARGET}",
    re.IGNORECASE,
)


def secret_extraction_request(text: str) -> tuple[str, ...]:
    """Detect only high-confidence attempts to obtain secret values, not security questions."""
    normalized = " ".join(text.casefold().replace("ё", "е").split())
    if not normalized or not any(
        pattern.search(normalized)
        for pattern in (
            _DIRECT_SECRET_REQUEST_RE,
            _SECRET_VALUE_REQUEST_RE,
            _SECRET_FILE_LINE_REQUEST_RE,
            _OWN_SECRET_REQUEST_RE,
        )
    ):
        return ()
    return tuple(
        sorted(
            kind
            for kind, pattern in _SECRET_REQUEST_TARGET_PATTERNS
            if re.search(pattern, normalized, re.IGNORECASE) is not None
        )
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
