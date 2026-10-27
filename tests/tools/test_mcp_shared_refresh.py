"""A shared MCP connection must refresh the adopting profile's search catalog."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tests.tools.test_mcp_multiplex_connection_keys import two_profiles  # noqa: F401


def test_shared_refresh_reaches_new_session_catalog(two_profiles, monkeypatch):
    import model_tools
    import tools.mcp_tool as core
    from tools import mcp_tool_discovery as discovery, mcp_tool_registration as registration
    from tools.mcp_tool import MCPServerTask
    from tools.registry import registry

    def tool(name):
        return SimpleNamespace(name=name, description=name,
                               inputSchema={"type": "object", "properties": {}}, annotations=None)

    owner_config = {"url": "https://mcp.example/x",
                    "tools": {"resources": True, "prompts": False, "exclude": ["blocked"]}}
    adopter_config = {**owner_config, "tools": {**owner_config["tools"], "resources": False}}
    owner_scope = two_profiles("a")
    server = MCPServerTask("x")
    server._config = owner_config
    server._tools = [tool("existing")]
    server.session = SimpleNamespace(list_tools=AsyncMock(), list_resources=AsyncMock())
    server._resolved_identity = registration._adopter_identity_digest("x", owner_config)
    discovery._adopt_server("x", server)
    server._registered_tool_names = registration._register_server_tools("x", server, owner_config)

    adopter_scope = two_profiles("b")
    monkeypatch.setattr("tools.mcp_tool_config._load_mcp_config", lambda: {"x": adopter_config})
    assert registration.register_connected_into_current_scope({"x": adopter_config}) == 1
    enabled = ["mcp-x"]

    def definitions():
        return model_tools.get_tool_definitions(enabled_toolsets=enabled, quiet_mode=True,
                                                skip_tool_search_assembly=True)

    assert {d["function"]["name"] for d in definitions()} == {"mcp__x__existing"}
    definitions()  # Warm both tool-definition and toolset-resolution memos.
    generation = registry._generation
    server.session.list_tools.return_value = SimpleNamespace(
        tools=[tool("existing"), tool("abbhinnavs_mac_studio__terminal"), tool("blocked")], nextCursor=None)
    # The background callback belongs to the connection owner, not the adopting session.
    token = set_hermes_home_override(owner_scope)
    try:
        asyncio.run(server._refresh_tools())
    finally:
        reset_hermes_home_override(token)
    added = "mcp__x__abbhinnavs_mac_studio__terminal"
    assert added in server._registered_tool_names
    assert registry.get_entry(added, scope=owner_scope) is not None
    assert registry._generation > generation  # Invalidation works; publication is the bug.
    assert core._server_tool_scopes[(owner_scope, "x")] == {owner_scope, adopter_scope}
    names = {d["function"]["name"] for d in definitions()}
    assert "mcp__x__blocked" not in names  # Keep the adopter's own filter.
    assert added in names
    assert "mcp__x__list_resources" not in names
    assert registry.get_entry("mcp__x__list_resources", scope=owner_scope) is not None
    result, _ = model_tools._dispatch_bridge_tool(
        "tool_search", {"queries": ["abbhinnavs_mac_studio__terminal"]}, enabled, None)
    assert added in json.loads(result)["tools"]

    # Schema-only refreshes and removals must reach that same overlay too.
    changed = tool("existing")
    changed.description = "updated schema"
    server.session.list_tools.return_value = SimpleNamespace(tools=[changed], nextCursor=None)
    token = set_hermes_home_override(owner_scope)
    try:
        asyncio.run(server._refresh_tools())
    finally:
        reset_hermes_home_override(token)
    current = {d["function"]["name"]: d["function"] for d in definitions()}
    assert added not in current
    assert current["mcp__x__existing"]["description"] == "updated schema"
