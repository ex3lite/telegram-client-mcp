import pytest

from dca.config import Settings
from dca.db import Database
from dca.mcp import build_mcp


@pytest.mark.asyncio
async def test_mcp_tool_contracts_are_stable() -> None:
    settings = Settings(public_url="https://agent.example.com")
    database = Database(settings)
    try:
        server = build_mcp(settings, database)
        tools = {tool.name: tool for tool in await server.list_tools()}
    finally:
        await database.close()

    assert set(tools) == {
        "identity_resolve_user",
        "telegram_ask_user",
        "telegram_get_clarification",
        "telegram_cancel_clarification",
    }
    assert tools["identity_resolve_user"].inputSchema["required"] == ["project_id", "query"]
    ask_schema = tools["telegram_ask_user"].inputSchema
    assert ask_schema["required"] == ["request"]
    request_schema = ask_schema["$defs"]["AskUserInput"]
    assert request_schema["properties"]["project_id"]["format"] == "uuid"
    assert request_schema["properties"]["expires_at"]["format"] == "date-time"
    assert "idempotency_key" in request_schema["required"]
    assert server.settings.transport_security.allowed_hosts == ["agent.example.com"]
    assert server.settings.transport_security.allowed_origins == ["https://agent.example.com"]
