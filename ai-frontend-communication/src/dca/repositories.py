from __future__ import annotations

import hashlib
import hmac
import re
from urllib.parse import urlsplit

GITHUB_REPOSITORY_RE = re.compile(
    r"^(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9.-]{0,38}))/(?P<repo>[A-Za-z0-9_.-]{1,100})$"
)


def normalize_github_repository(value: str) -> str:
    normalized = value.strip().strip("/").removesuffix(".git")
    if GITHUB_REPOSITORY_RE.fullmatch(normalized) is None:
        raise ValueError("GitHub repository must use owner/name format")
    return normalized.casefold()


def github_repository_from_url(value: str) -> str | None:
    source = value.strip()
    if source.startswith("git@github.com:"):
        candidate = source.removeprefix("git@github.com:")
    else:
        parsed = urlsplit(source)
        if (parsed.hostname or "").casefold() != "github.com":
            return None
        candidate = parsed.path
    try:
        return normalize_github_repository(candidate)
    except ValueError:
        return None


def github_webhook_signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_github_webhook_signature(secret: str, body: bytes, supplied: str | None) -> bool:
    if supplied is None or re.fullmatch(r"sha256=[0-9a-f]{64}", supplied) is None:
        return False
    return hmac.compare_digest(github_webhook_signature(secret, body), supplied)
