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
_INTERNAL_SERVER_PATH_RE = re.compile(
    r"(?<![\w/])/(?:etc|opt|srv|root|home|run|mnt|var/(?:lib|log|backups?))"
    r"(?:/[A-Za-z0-9._~@%+=:,${}-]+)+"
)
_ENV_IDENTIFIER_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_SENSITIVE_ENV_IDENTIFIER_RE = re.compile(
    r"\b(?:[A-Z][A-Z0-9]*_)*(?:PASSWORD|PASSWD|SECRET|TOKEN|PRIVATE_KEY|ACCESS_KEY|"
    r"SIGNING_KEY|API_KEY|HMAC_KEY|PEPPER|CREDENTIALS?|DATABASE_URL|REDIS_URL|PROXY_URL)\b"
)
_ENV_INVENTORY_CONTEXT_RE = re.compile(
    r"(?<![\w.])(?:\.env(?:\.[a-z0-9_-]+)?|[a-z0-9_-]+\.env)(?![\w.])|"
    r"\b(?:env(?:ironment)?\s+var(?:iable)?s?|environment\s+variables?)\b|"
    r"\bпеременн\w*\s+окружени\w*\b|\benv\b",
    re.IGNORECASE,
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
        "cloud_access_key",
        r"(?:(?:s3|aws|selectel|селектел)[- _]?(?:access[- _]?)?"
        r"(?:key|ключ(?:а|у|ом|е|и|ей|ами|ах)?)|"
        r"(?:key|ключ(?:а|у|ом|е|и|ей|ами|ах)?)\s+(?:от\s+)?"
        r"(?:s3|aws|selectel|селектел))",
    ),
    (
        "token",
        r"(?:access|refresh|bearer|auth|oauth|telegram|bot|доступа|бота)?[- _]?"
        r"(?:token|токен(?:а|у|ом|е|ы|ов|ам|ами|ах)?)",
    ),
    ("password", r"(?:password|passwd|парол(?:ь|я|ю|ем|е|и|ей|ям|ями|ях))"),
    (
        "credentials",
        r"(?:credentials?|credential|креды|кредов|учетные\s+данные|"
        r"(?:параметры|данные)\s+доступа|access\s+(?:parameters?|details?))",
    ),
    ("secret", r"(?:secrets?|секрет(?:а|у|ом|е|ы|ов|ам|ами|ах)?)"),
    (
        "environment_file",
        r"(?<![\w.])\.env(?:\.[a-z0-9_-]+)?(?![\w.])|"
        r"(?:env|environment)[- _]?(?:file|файл)|(?<![\w.])env(?![\w.])",
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
    r"скидывай|сливай|отдай|отдайте|передай|передайте|предоставь|предоставьте|"
    r"выведи|выведите|напечатай|напечатайте|покажи|покажите|верни|верните|"
    r"прочитай|прочитайте|достань|достаньте|скопируй|скопируйте|"
    r"give|send|provide|dump|reveal|print|show|output|extract|read|leak|copy)"
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
_SECRET_FILE_LINE_REQUEST_RE = re.compile(
    rf"\b(?:первая|первую|последняя|последнюю|вторая|вторую|first|last|second)\s+"
    rf"(?:строка|строку|line)\b.{{0,60}}{_SECRET_REQUEST_TARGET}",
    re.IGNORECASE,
)
_OWN_SECRET_REQUEST_RE = re.compile(
    rf"\b(?:какой|какая|какое|what\s+is)\b\s+"
    rf"(?:(?:значение|the\s+value\s+of|value\s+of)\s+)?"
    rf"(?:(?:у\s+нас|наш|наша|текущий|боевой|продовый|our|current|production|prod)\s+)+"
    rf"{_SECRET_REQUEST_TARGET}",
    re.IGNORECASE,
)
_INDIRECT_SECRET_REQUEST_RE = re.compile(
    rf"(?:\b(?:можешь|хочешь|не\s+хочешь|could\s+you|would\s+you)\b.{{0,50}}"
    rf"\b(?:дать|скинуть|прислать|показать|give|send|show)\b.{{0,50}}"
    rf"{_SECRET_REQUEST_TARGET}|"
    rf"\b(?:мне|нам|i|we)\b.{{0,50}}\b(?:надо|нужно|необходимо|need|must)\b"
    rf".{{0,30}}\b(?:получить|достать|get|obtain)\b.{{0,40}}{_SECRET_REQUEST_TARGET})",
    re.IGNORECASE,
)
_TARGET_FIRST_SECRET_REQUEST_RE = re.compile(
    rf"{_SECRET_REQUEST_TARGET}.{{0,40}}\b(?:скидывай|сливай|отдавай|выводи|"
    rf"dump|send|show|output)\b",
    re.IGNORECASE,
)
_SOCIAL_ENGINEERING_SECRET_RE = re.compile(
    r"(?:\b(?:дай|скинь|пришли|получить|достать|нужен|нужна|give|send|obtain|need)\b"
    r".{0,50}\b(?:ключ\w*|парол\w*|токен\w*|доступ\w*|key|password|token|access)\b"
    r".{0,180}\b(?:угрож\w*|жизн\w*|семь\w*|убью|выреж\w*|threat\w*|life|family)\b|"
    r"\b(?:угрож\w*|жизн\w*|семь\w*|убью|выреж\w*|threat\w*|life|family)\b"
    r".{0,180}\b(?:дай|скинь|пришли|получить|достать|нужен|нужна|give|send|obtain|need)\b"
    r".{0,50}\b(?:ключ\w*|парол\w*|токен\w*|доступ\w*|key|password|token|access)\b)",
    re.IGNORECASE,
)
_SECRET_TRANSFORM_REQUEST_RE = re.compile(
    rf"(?:\b(?:покажи|выведи|пришли|дай|напиши|закодируй|переведи|разверни|"
    rf"show|output|send|give|encode|convert|reverse)\b.{{0,100}}"
    rf"\b(?:перв\w*|последн\w*|символ\w*|байт\w*|част\w*|base64|hex|"
    rf"задом\s+наперед|reverse|chars?|bytes?|part)\b.{{0,100}}"
    rf"{_SECRET_REQUEST_TARGET}|"
    rf"\b(?:покажи|выведи|пришли|дай|напиши|show|output|send|give|write)\b"
    rf".{{0,80}}{_SECRET_REQUEST_TARGET}.{{0,80}}\b(?:перв\w*|последн\w*|"
    rf"символ\w*|байт\w*|част\w*|base64|hex|задом\s+наперед|reverse|"
    rf"chars?|bytes?|part)\b|"
    rf"\b(?:закодируй|переведи|разверни|encode|convert|reverse)\b.{{0,80}}"
    rf"{_SECRET_REQUEST_TARGET}.{{0,80}}\b(?:base64|hex|задом\s+наперед|reverse)\b)",
    re.IGNORECASE,
)
_SYNTHETIC_SECRET_CONTEXT_RE = re.compile(
    r"\b(?:placeholder|dummy|fake|synthetic|mock|плейсхолдер|заглушк\w*|"
    r"фейков\w*|тестов\w*|пример\w*)\b",
    re.IGNORECASE,
)
_REAL_SECRET_CONTEXT_RE = re.compile(
    r"\b(?:prod|production|current|actual|real|боев\w*|продов\w*|"
    r"текущ\w*|реальн\w*|наш\w*|your|our)\b",
    re.IGNORECASE,
)

_PROMPT_OVERRIDE_RE = re.compile(
    r"\b(?:ignore|disregard|forget|override|bypass|игнорируй|игнорировать|"
    r"забудь|забыть|отмени|обойди)\b.{0,100}\b(?:all|any|previous|prior|system|"
    r"developer|security|safety|policy|rules?|instructions?|prompt|guard|все|"
    r"любые|предыдущ\w*|системн\w*|инструкц\w*|правил\w*|политик\w*|"
    r"безопасност\w*|ограничен\w*|промпт\w*|защит\w*)\b",
    re.IGNORECASE,
)
_SYSTEM_PROMPT_REQUEST_RE = re.compile(
    rf"\b{_SECRET_REQUEST_ACTION}\b.{{0,80}}\b(?:свой|твой|ваш|текущий|скрытый|"
    r"полный|your|current|hidden|full)\b.{0,40}\b(?:system[- _]?prompt|"
    r"developer[- _]?message|base[- _]?prompt|системн\w*\s+промпт\w*|"
    r"скрыт\w*\s+инструкц\w*)\b",
    re.IGNORECASE,
)
_PERMISSION_BYPASS_RE = re.compile(
    r"(?:\b(?:сними|отключи|убери|обойди|disable|remove|bypass)\b.{0,70}"
    r"\b(?:read[- ]?only|sandbox|guard|policy|permissions?|ограничен\w*|"
    r"запрет\w*|песочниц\w*|защит\w*|политик\w*|прав\w*)\b|"
    r"\b(?:дай|включи|выдай|grant|enable)\b.{0,60}\b(?:write[- ]?access|root|"
    r"admin|полный\s+доступ|права\s+(?:записи|админ\w*))\b|"
    r"\b(?:теперь\s+ты|ты\s+теперь|act\s+as|pretend\s+(?:that\s+)?you(?:'re|\s+are)|"
    r"представь,?\s+что\s+ты)\b.{0,60}\b(?:admin|root|system|backend[- ]?admin|"
    r"админ\w*|системн\w*\s+пользовател\w*|рут\w*)\b|"
    r"\b(?:pretend|assume|представь|считай)\b.{0,100}\b(?:have|has|есть|имеешь)\b"
    r".{0,40}\b(?:access|доступ)\b)",
    re.IGNORECASE,
)
_COMMAND_EXECUTION_RE = re.compile(
    r"\b(?:запусти|выполни|исполни|прогони|вызови|run|execute)\b.{0,120}"
    r"\b(?:команд\w*|shell|bash|sudo|systemctl|docker|kubectl|python|node|psql|"
    r"curl|wget|git\s+(?:push|commit)|drop\s+table|truncate\s+table|"
    r"тест\w*|tests?|миграц\w*|api|endpoint|на\s+(?:сервере|проде)|"
    r"server|production)\b",
    re.IGNORECASE,
)
_STATE_MUTATION_RE = re.compile(
    r"(?:\b(?:задеплой|выкати|запушь|закоммить|перезапусти)\b|"
    r"^(?:please\s+)?(?:deploy|push|commit|restart)\b|"
    r"\b(?:создай|удали|измени|запиши|перезапиши|смени|сбрось|добавь|"
    r"create|delete|remove|edit|write|change|reset|add)\b.{0,80}"
    r"\b(?:пользовател\w*|аккаунт\w*|парол\w*|прав\w*|permission\w*|user\w*|"
    r"account\w*|password\w*|файл\w*|директор\w*|репозитор\w*|ветк\w*|"
    r"баз\w*\s+данн\w*|таблиц\w*|server|service|systemd|production|prod|"
    r"сервер\w*|сервис\w*|прод\w*)\b|"
    r"\b(?:сделай|do)\b.{0,50}\b(?:git\s+(?:push|commit)|deploy|деплой)\b)",
    re.IGNORECASE,
)
_RESTRICTED_PATH = (
    r"(?<![\w/])(?:\.\./|/(?:etc|root|srv|opt|home|run|mnt|"
    r"var(?:/(?:lib|log|backups?))?)(?:/[^\s\"'<>]*)?)"
)
_SANDBOX_ESCAPE_RE = re.compile(
    rf"(?:\b(?:открой|прочитай|покажи|выведи|найди|перечисли|просканируй|"
    rf"open|read|show|list|scan)\b.{{0,100}}{_RESTRICTED_PATH}|"
    rf"\b(?:попробуй|try)\b.{{0,30}}\b(?:открыть|прочитать|посмотреть|open|read)\b"
    rf".{{0,80}}{_RESTRICTED_PATH}|"
    rf"\b(?:cat|ls|find)\b.{{0,20}}{_RESTRICTED_PATH}|"
    r"\b(?:выйди|выйти|лезь|залезь|просканируй|scan|escape)\b.{0,100}"
    r"\b(?:за\s+предел\w*|вне|соседн\w*\s+репозитор\w*|друг\w*\s+папк\w*|"
    r"outside|other\s+repositor\w*|parent\s+director\w*)\b)",
    re.IGNORECASE,
)
_AUDIT_BYPASS_RE = re.compile(
    r"(?:\bне\b.{0,15}\b(?:логируй|записывай|фиксируй|log|record)\b.{0,50}"
    r"\b(?:этот|это|запрос|попытк\w*|меня|this|request|attempt)\b|"
    r"\b(?:отключи|обойди|скрой|сотри|disable|bypass|hide|erase)\b.{0,80}"
    r"\b(?:аудит\w*|audit|лог\w*|logs?|bydlo[- _]?guard|guard|защит\w*)\b)",
    re.IGNORECASE,
)
_PRIVATE_INFRA_REQUEST_RE = re.compile(
    rf"\b{_SECRET_REQUEST_ACTION}\b.{{0,80}}\b(?:внутрянк\w*|"
    r"(?:private|internal)\s+(?:infra(?:structure)?|topology)|"
    r"доступ\s+(?:к|на)\s+(?:test_ai|прод\w*\s+сервер\w*|сервер\w*))\b",
    re.IGNORECASE,
)
_REVIEW_EXAMPLE_PREFIX_RE = re.compile(
    r"^(?:проверь|проанализируй|оцени|сделай\s+ревью|review|audit)\b.{0,100}"
    r"\b(?:код\w*|фильтр\w*|детектор\w*|regex|регуляр\w*|валидац\w*|"
    r"скрипт\w*|сообщени\w*|input|запрос\w*|строк\w*)\b",
    re.IGNORECASE,
)
_QUOTED_EXAMPLE_RE = re.compile(
    r"```.*?```|`[^`]+`|«[^»]+»|“[^”]+”|\"[^\"]+\"|'[^']+'",
    re.DOTALL,
)


def _normalize_request(text: str) -> str:
    without_invisible = text.translate(
        {
            ord("\u200b"): None,
            ord("\u200c"): None,
            ord("\u200d"): None,
            ord("\ufeff"): None,
        }
    )
    return " ".join(without_invisible.casefold().replace("ё", "е").split())


def secret_extraction_request(text: str) -> tuple[str, ...]:
    """Detect only high-confidence attempts to obtain secret values, not security questions."""
    normalized = _normalize_request(text)
    if _SYNTHETIC_SECRET_CONTEXT_RE.search(normalized) and not _REAL_SECRET_CONTEXT_RE.search(
        normalized
    ):
        return ()
    if not normalized or not any(
        pattern.search(normalized)
        for pattern in (
            _DIRECT_SECRET_REQUEST_RE,
            _SECRET_FILE_LINE_REQUEST_RE,
            _OWN_SECRET_REQUEST_RE,
            _INDIRECT_SECRET_REQUEST_RE,
            _TARGET_FIRST_SECRET_REQUEST_RE,
            _SECRET_TRANSFORM_REQUEST_RE,
            _SOCIAL_ENGINEERING_SECRET_RE,
        )
    ):
        return ()
    kinds = {
        kind
        for kind, pattern in _SECRET_REQUEST_TARGET_PATTERNS
        if re.search(pattern, normalized, re.IGNORECASE) is not None
    }
    if _SOCIAL_ENGINEERING_SECRET_RE.search(normalized):
        kinds.add("secret")
    return tuple(sorted(kinds))


def guard_request_kinds(text: str) -> tuple[str, ...]:
    """Classify explicit attempts to make the read-only agent cross a security boundary."""
    normalized = _normalize_request(text)
    if not normalized:
        return ()
    intent_text = (
        _QUOTED_EXAMPLE_RE.sub(" ", normalized)
        if _REVIEW_EXAMPLE_PREFIX_RE.search(normalized)
        else normalized
    )
    kinds = set(secret_extraction_request(intent_text))
    checks = (
        ("prompt_injection", (_PROMPT_OVERRIDE_RE, _SYSTEM_PROMPT_REQUEST_RE)),
        ("permission_bypass", (_PERMISSION_BYPASS_RE,)),
        ("command_execution", (_COMMAND_EXECUTION_RE,)),
        ("state_mutation", (_STATE_MUTATION_RE,)),
        ("sandbox_escape", (_SANDBOX_ESCAPE_RE,)),
        ("audit_bypass", (_AUDIT_BYPASS_RE,)),
        ("private_infrastructure", (_PRIVATE_INFRA_REQUEST_RE,)),
    )
    for kind, patterns in checks:
        if any(pattern.search(intent_text) for pattern in patterns):
            kinds.add(kind)
    return tuple(sorted(kinds))


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


def sanitize_agent_output(text: str, *, level: PrivacyLevel, location: str) -> PrivacyResult:
    """Hide secret values plus internal paths and environment metadata from agent output."""
    result = sanitize_text(text, level=level, location=location)
    if result.blocked:
        return result
    identifiers = list(_ENV_IDENTIFIER_RE.finditer(result.text))
    redact_inventory = _ENV_INVENTORY_CONTEXT_RE.search(result.text) is not None
    spans = {
        (match.start(), match.end(), "internal_server_path")
        for match in _INTERNAL_SERVER_PATH_RE.finditer(result.text)
    }
    spans.update(
        (match.start(), match.end(), "environment_metadata")
        for match in identifiers
        if redact_inventory or _SENSITIVE_ENV_IDENTIFIER_RE.fullmatch(match.group())
    )
    if not spans:
        return result

    chunks: list[str] = []
    findings = list(result.findings)
    cursor = 0
    for start, end, kind in sorted(spans):
        if start < cursor:
            continue
        chunks.extend((result.text[cursor:start], f"[REDACTED:{kind}]"))
        cursor = end
        findings.append({"kind": kind, "location": location, "action": "redacted"})
    chunks.append(result.text[cursor:])
    return PrivacyResult(text="".join(chunks), findings=findings, blocked=result.blocked)
