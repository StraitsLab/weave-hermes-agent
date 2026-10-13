import asyncio
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from tools import mcp_tool
from tools.registry import ToolRegistry


def test_refresh_updates_schemas_and_removals_on_mcp_loop():
    registry = ToolRegistry()
    mcp_tool._ensure_mcp_loop()
    async def setup():
        server = mcp_tool.MCPServerTask("weave-connectors")
        async def listing(**kwargs):
            assert asyncio.get_running_loop() is mcp_tool._mcp_loop
            return SimpleNamespace(tools=[SimpleNamespace(name="kept", description="new", inputSchema={"type": "object", "properties": {"new": {"type": "string"}}}), SimpleNamespace(name="added", description="added", inputSchema={"type": "object"})])
        server.session = SimpleNamespace(list_tools=listing)
        server._ready.set()
        server._tools = [SimpleNamespace(name=name, description="old", inputSchema={"type": "object"}) for name in ("kept", "removed")]
        server._registered_tool_names = mcp_tool._register_server_tools(server.name, server, {})
        return server
    with patch("tools.registry.registry", registry):
        server = mcp_tool._run_on_mcp_loop(setup)
        with patch.dict(mcp_tool._servers, {"weave-connectors": server, "exa": object()}, clear=True):
            names = mcp_tool.refresh_mcp_server_tools("weave-connectors")
            assert set(names) == {"mcp__weave_connectors__kept", "mcp__weave_connectors__added"}
            assert "mcp__weave_connectors__removed" not in registry.get_all_tool_names()
            assert registry.get_schema("mcp__weave_connectors__kept")["description"] == "new"
            assert server._tools[0].description == "new"
            assert "new" in server._tools[0].inputSchema["properties"]
            server._ready.clear()
            with pytest.raises(RuntimeError):
                mcp_tool.refresh_mcp_server_tools("weave-connectors")
        with patch.dict(mcp_tool._servers, {}, clear=True), pytest.raises(RuntimeError):
            mcp_tool.refresh_mcp_server_tools("weave-connectors")


@pytest.mark.parametrize("failure", ["rpc", "disconnect", "no_tools"])
def test_refresh_failure_keeps_previous_registry(failure):
    registry = ToolRegistry()
    mcp_tool._ensure_mcp_loop()
    async def setup():
        server = mcp_tool.MCPServerTask("weave-connectors")
        async def listing(**kwargs):
            if failure == "rpc":
                raise RuntimeError("list failed")
            server._ready.clear()
            return SimpleNamespace(tools=[])
        server.session = SimpleNamespace(list_tools=listing)
        server._ready.set()
        server._tools = [SimpleNamespace(name="kept", description="old", inputSchema={"type": "object"})]
        server._registered_tool_names = mcp_tool._register_server_tools(server.name, server, {})
        if failure == "no_tools":
            server.initialize_result = SimpleNamespace(capabilities=SimpleNamespace(tools=None))
        return server
    with patch("tools.registry.registry", registry):
        server = mcp_tool._run_on_mcp_loop(setup)
        with patch.dict(mcp_tool._servers, {"weave-connectors": server}, clear=True):
            with pytest.raises(RuntimeError):
                mcp_tool.refresh_mcp_server_tools("weave-connectors")
            assert registry.get_all_tool_names() == ["mcp__weave_connectors__kept"]


def test_scoped_refresh_preserves_exa_connection_and_schema():
    """Connection correctness is independent of agent snapshot adoption."""
    from unittest.mock import AsyncMock
    registry = ToolRegistry()
    mcp_tool._ensure_mcp_loop()

    async def setup():
        connector = mcp_tool.MCPServerTask("weave-connectors")
        connector.session = SimpleNamespace(list_tools=AsyncMock(return_value=SimpleNamespace(tools=[SimpleNamespace(name="kept", description="new", inputSchema={"type": "object"})])))
        connector._ready.set()
        connector._tools = [SimpleNamespace(name="kept", description="old", inputSchema={"type": "object"})]
        connector._registered_tool_names = mcp_tool._register_server_tools(connector.name, connector, {})
        exa = mcp_tool.MCPServerTask("exa")
        exa.session = SimpleNamespace(list_tools=AsyncMock())
        exa._ready.set()
        exa._tools = [SimpleNamespace(name="search", description="Exa search", inputSchema={"type": "object"})]
        exa._registered_tool_names = mcp_tool._register_server_tools(exa.name, exa, {})
        return connector, exa

    with patch("tools.registry.registry", registry), patch.dict(mcp_tool._mcp_tool_server_names, {}, clear=True):
        connector, exa = mcp_tool._run_on_mcp_loop(setup)
        exa_schema = registry.get_schema("mcp__exa__search")
        exa_session = exa.session
        with patch.dict(mcp_tool._servers, {"weave-connectors": connector, "exa": exa}, clear=True), patch("tools.mcp_tool._connect_server") as connect, patch("tools.mcp_tool.shutdown_mcp_servers") as shutdown, patch("tools.mcp_tool.discover_mcp_tools") as discover:
            assert mcp_tool.refresh_mcp_server_tools("weave-connectors") == ["mcp__weave_connectors__kept"]
            assert mcp_tool._servers["exa"] is exa
            assert exa.session is exa_session
            assert registry.get_schema("mcp__exa__search") is exa_schema
            exa.session.list_tools.assert_not_called()
            connect.assert_not_called()
            shutdown.assert_not_called()
            discover.assert_not_called()
