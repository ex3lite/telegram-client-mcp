import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from dca.app import create_app
from dca.config import Settings

TEST_ADMIN_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$VOSDEqLqzDdishlC6gzomg$"
    "m/ACEM4oYjjuHoPfiSTj14NOld3UMUKR30ePVYQ97gM"
)


@pytest.mark.asyncio
async def test_admin_dependency_is_a_cookie_not_a_query_parameter(tmp_path) -> None:
    app = create_app(
        Settings(
            cookie_secure=False,
            admin_password_hash=SecretStr(TEST_ADMIN_HASH),
            session_secret=SecretStr("s" * 32),
            repository_root=tmp_path / "repositories",
            snapshot_root=tmp_path / "snapshots",
        )
    )

    operation = app.openapi()["paths"]["/api/v1/auth/me"]["get"]
    assert len(operation["parameters"]) == 1
    assert operation["parameters"][0]["name"] == "dca_admin"
    assert operation["parameters"][0]["in"] == "cookie"

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/api/v1/auth/me")
    finally:
        await app.state.telegram.close()
        await app.state.redis.aclose()
        await app.state.database.close()
    assert response.status_code == 401
    assert response.json() == {"detail": "authentication_required"}


@pytest.mark.asyncio
async def test_mcp_endpoint_and_protected_resource_metadata_are_publicly_aligned(tmp_path) -> None:
    app = create_app(
        Settings(
            public_url="http://testserver",
            repository_root=tmp_path / "repositories",
            snapshot_root=tmp_path / "snapshots",
        )
    )
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            metadata = await client.get("/.well-known/oauth-protected-resource/mcp")
            handshake = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "contract-test", "version": "1"},
                    },
                },
            )
    finally:
        await app.state.telegram.close()
        await app.state.redis.aclose()
        await app.state.database.close()

    assert metadata.status_code == 200
    assert metadata.json()["resource"] == "http://testserver/mcp"
    assert handshake.status_code == 401
    assert handshake.history == []


@pytest.mark.asyncio
async def test_unconfigured_admin_cannot_use_a_forged_fallback_cookie(tmp_path) -> None:
    app = create_app(
        Settings(
            repository_root=tmp_path / "repositories",
            snapshot_root=tmp_path / "snapshots",
        )
    )
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
            cookies={"dca_admin": "attacker-controlled"},
        ) as client:
            response = await client.get("/api/v1/auth/me")
    finally:
        await app.state.telegram.close()
        await app.state.redis.aclose()
        await app.state.database.close()

    assert response.status_code == 503
    assert response.json() == {"detail": "admin_auth_not_configured"}
