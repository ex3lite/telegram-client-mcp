import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError

from dca.app import create_app
from dca.config import Settings
from dca.repositories import (
    github_repository_from_url,
    github_webhook_signature,
    normalize_github_repository,
    verify_github_webhook_signature,
)


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:Matrena-VPN/backend_ai.git",
        "ssh://git@github.com/Matrena-VPN/backend_ai.git",
        "https://github.com/Matrena-VPN/backend_ai",
    ],
)
def test_github_repository_identity_is_normalized_from_supported_urls(url: str) -> None:
    assert github_repository_from_url(url) == "matrena-vpn/backend_ai"


def test_github_webhook_signature_is_exact_and_constant_time_checked() -> None:
    secret = "s" * 32
    body = b'{"ref":"refs/heads/main"}'
    signature = github_webhook_signature(secret, body)

    assert verify_github_webhook_signature(secret, body, signature) is True
    assert verify_github_webhook_signature(secret, body + b" ", signature) is False
    assert verify_github_webhook_signature(secret, body, None) is False
    assert verify_github_webhook_signature(secret, body, "sha256=" + "g" * 64) is False
    assert verify_github_webhook_signature(secret, body, "sha256=" + "é" * 64) is False


@pytest.mark.asyncio
async def test_github_webhook_rejects_non_ascii_signature(tmp_path) -> None:
    settings = Settings(
        public_url="http://testserver",
        session_secret=SecretStr("test-session-secret-with-32-characters"),
        cookie_secure=False,
        github_webhook_secret=SecretStr("g" * 32),
        repository_root=tmp_path / "repositories",
        snapshot_root=tmp_path / "snapshots",
    )
    app = create_app(settings)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/webhooks/github",
                content=b"{}",
                headers=[(b"X-Hub-Signature-256", "sha256=é".encode())],
            )
        assert response.status_code == 401
        assert response.json()["detail"] == "invalid_webhook_signature"
    finally:
        await app.state.telegram.close()
        await app.state.redis.aclose()
        await app.state.database.close()


def test_github_repository_and_secret_reject_ambiguous_configuration() -> None:
    with pytest.raises(ValueError):
        normalize_github_repository("github.com/owner/repository")
    with pytest.raises(ValidationError):
        Settings(github_webhook_secret=SecretStr("short"))


@pytest.mark.asyncio
async def test_github_webhook_caps_chunked_body_without_content_length(tmp_path) -> None:
    settings = Settings(
        public_url="http://testserver",
        session_secret=SecretStr("test-session-secret-with-32-characters"),
        cookie_secure=False,
        github_webhook_secret=SecretStr("g" * 32),
        max_github_webhook_body_bytes=1_024,
        repository_root=tmp_path / "repositories",
        snapshot_root=tmp_path / "snapshots",
    )
    app = create_app(settings)

    async def oversized_body():
        yield b"x" * 600
        yield b"y" * 600

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/webhooks/github",
                content=oversized_body(),
                headers={"Transfer-Encoding": "chunked"},
            )
        assert response.status_code == 413
        assert response.json()["detail"] == "payload_too_large"
    finally:
        await app.state.telegram.close()
        await app.state.redis.aclose()
        await app.state.database.close()
