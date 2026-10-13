"""Scoped registry refresh reaches a native TUI session at its next turn."""
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tools import mcp_tool
from tools.registry import ToolRegistry
from tui_gateway import server
from tests.agent.test_turn_context import _FakeAgent, _build
from tests.gateway.test_mcp_scoped_reload import request
from tests.gateway.test_api_server_mcp_reload import _make_adapter, _make_bare_runner
from tests.tui_gateway.test_prompt_accept_logging import _session, turn_env


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["description", "inputSchema", "both"])
async def test_same_named_schema_reaches_native_next_turn(change):
    registry = ToolRegistry()
    old = SimpleNamespace(name="kept", description="old description", inputSchema={"type": "object", "properties": {"old": {"type": "string"}}})
    fresh = SimpleNamespace(name="kept", description="new description" if change != "inputSchema" else old.description, inputSchema={"type": "object", "properties": {"new": {"type": "integer"}}} if change != "description" else old.inputSchema)
    mcp_tool._ensure_mcp_loop()

    async def setup():
        connector = mcp_tool.MCPServerTask("weave-connectors")
        connector.session = SimpleNamespace(list_tools=AsyncMock(return_value=SimpleNamespace(tools=[fresh])))
        connector._ready.set()
        connector._tools = [old]
        connector._registered_tool_names = mcp_tool._register_server_tools(connector.name, connector, {})
        return connector

    with patch("tools.registry.registry", registry), patch.dict(mcp_tool._mcp_tool_server_names, {}, clear=True):
        connector = mcp_tool._run_on_mcp_loop(setup)
        name = "mcp__weave_connectors__kept"
        agent = _FakeAgent()
        agent.tools = [{"type": "function", "function": copy.deepcopy(registry.get_schema(name))}]
        agent.valid_tool_names = {name}
        agent.enabled_toolsets = ["mcp-weave-connectors"]
        agent.disabled_toolsets = ["terminal"]
        stale = agent.tools
        runner = _make_bare_runner()
        assert not runner._agent_cache  # native session is not owned by GatewayRunner
        with patch.dict(mcp_tool._servers, {"weave-connectors": connector}, clear=True):
            response = await _make_adapter(runner)._handle_mcp_refresh(request())
            assert response.status == 200
            assert registry.get_all_tool_names() == [name]  # no adds/removes
            assert registry.get_schema(name)["description"] == fresh.description
            assert registry.get_schema(name)["parameters"] == fresh.inputSchema
            assert agent.tools is stale  # in-flight prefix was not rebuilt
            expected = [{"type": "function", "function": copy.deepcopy(registry.get_schema(name))}]
            with patch("model_tools.get_tool_definitions", return_value=expected) as definitions:
                _build(agent)
            assert agent.tools == expected
            assert agent.valid_tool_names == {name}
            assert definitions.call_args.kwargs["enabled_toolsets"] == ["mcp-weave-connectors"]
            assert definitions.call_args.kwargs["disabled_toolsets"] == ["terminal"]


def test_native_tui_dispatch_uses_turn_prologue(turn_env):
    """Exercise the actual TUI dispatch and real turn prologue, not runner cache."""
    agent = _FakeAgent()
    name = "mcp__weave_connectors__kept"
    old = [{"type": "function", "function": {"name": name, "description": "old", "parameters": {"type": "object"}}}]
    fresh = [{"type": "function", "function": {"name": name, "description": "new", "parameters": {"type": "object", "properties": {"new": {"type": "integer"}}}}}]
    agent.tools = old
    agent.valid_tool_names = {name}
    agent.context_compressor.threshold_tokens = 1000000
    agent.context_compressor.context_length = 1000000
    captures = []

    def run_conversation(message, **kwargs):
        _build(agent, user_message=message, conversation_history=kwargs.get("conversation_history"))
        captures.append(copy.deepcopy(agent.tools))
        return {"final_response": "done"}

    agent.run_conversation = run_conversation
    agent.clear_interrupt = lambda: None
    session = _session(agent=agent, running=True)
    with patch.dict(server._sessions, {"ui-sid": session}, clear=True), patch("tools.mcp_tool.has_registered_mcp_tools", return_value=True), patch("model_tools.get_tool_definitions", return_value=fresh):
        server._run_prompt_submit("rid", "ui-sid", session, "next turn")
    assert captures == [fresh]
    assert session["agent"] is agent
