import pytest

from dca.config import Settings
from dca.db import Database, ProjectMembership, User
from dca.mcp import build_mcp
from dca.service import project_member_profile


def test_member_profile_exposes_project_department_and_stack() -> None:
    profile = project_member_profile(
        User(display_name="Игорь"),
        ProjectMembership(role="developer", department="Mobile", stack="iOS / Swift"),
    )

    assert profile == {
        "display_name": "Игорь",
        "role": "developer",
        "department": "Mobile",
        "stack": "iOS / Swift",
        "language": "ru",
        "knowledge_scope": "integration",
        "can_create_requests": True,
    }


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
        "telegram_send_message",
        "memory_get_context",
    }
    assert tools["identity_resolve_user"].inputSchema["required"] == ["project_id", "query"]
    ask_schema = tools["telegram_ask_user"].inputSchema
    assert ask_schema["required"] == ["request"]
    request_schema = ask_schema["$defs"]["AskUserInput"]
    assert request_schema["properties"]["project_id"]["format"] == "uuid"
    assert request_schema["properties"]["expires_at"]["format"] == "date-time"
    assert "idempotency_key" in request_schema["required"]
    send_schema = tools["telegram_send_message"].inputSchema
    assert send_schema["required"] == ["request"]
    send_request = send_schema["$defs"]["TelegramSendMessageInput"]
    assert send_request["properties"]["project_id"]["format"] == "uuid"
    assert send_request["properties"]["text_markdown"]["maxLength"] == 4096
    memory_schema = tools["memory_get_context"].inputSchema
    assert memory_schema["required"] == ["request"]
    memory_request = memory_schema["$defs"]["MemoryContextInput"]
    assert memory_request["properties"]["project_id"]["format"] == "uuid"
    assert memory_request["properties"]["message_limit"]["maximum"] == 100
    transport_security = server.settings.transport_security
    assert transport_security is not None
    assert transport_security.allowed_hosts == ["agent.example.com"]
    assert transport_security.allowed_origins == ["https://agent.example.com"]
