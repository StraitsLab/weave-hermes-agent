"""Next-turn intent of the old TUI scoped refresh, now through gateway's turn prologue."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.turn_context import _refresh_mcp_tools_between_turns
from tools import mcp_tool_discovery as discovery, mcp_tool_agent as maintenance


def definition(description='old', schema=None):
    return {'type': 'function', 'function': {'name': 'mcp__weave_connectors__lookup',
            'description': description, 'parameters': schema or {'type': 'object', 'properties': {}}}}


@pytest.mark.parametrize('description,schema', [('changed', None), ('old', {'type': 'object', 'required': ['id']}),
                                               ('changed', {'type': 'object', 'required': ['id']})])
def test_same_names_adopt_content_only_at_turn_boundary(monkeypatch, description, schema):
    import model_tools
    from tools import mcp_tool  # activate the real import-cost gate
    original = [definition()]
    agent = SimpleNamespace(tools=original, valid_tool_names={'mcp__weave_connectors__lookup'},
                            enabled_toolsets=['mcp-weave-connectors'], disabled_toolsets=None,
                            _tool_snapshot_generation=-1)
    fresh = [definition(description, schema)]
    get_defs = Mock(return_value=fresh)
    monkeypatch.setattr(model_tools, 'get_tool_definitions', get_defs)
    monkeypatch.setattr(discovery, 'has_registered_mcp_tools', lambda: True)
    monkeypatch.setattr(maintenance, '_reinject_post_build_tools', lambda *a: set())
    # Publishing a registry does not mutate an active agent's original snapshot.
    assert agent.tools is original and agent.tools[0]['function']['description'] == 'old'
    _refresh_mcp_tools_between_turns(agent)
    assert agent.tools == fresh
    assert get_defs.call_args.kwargs['enabled_toolsets'] == ['mcp-weave-connectors']


@pytest.mark.parametrize('description,schema', [('changed', None),
    ('old', {'type': 'object', 'properties': {'id': {'type': 'string'}}, 'required': ['id']}),
    ('changed', {'type': 'object', 'properties': {'id': {'type': 'string'}}, 'required': ['id']})])
def test_connected_refresh_publishes_real_registry_before_turn_adopts(monkeypatch, description, schema):
    import asyncio
    from unittest.mock import AsyncMock
    import model_tools
    from tools import mcp_tool as core, mcp_tool_harso as harso
    from tools.mcp_tool_registration import _register_server_tools
    from tools.registry import ToolRegistry
    registry = ToolRegistry()
    monkeypatch.setattr('tools.registry.registry', registry)
    server = core.MCPServerTask('weave-connectors')
    def tool(desc, params):
        return SimpleNamespace(name='lookup', description=desc, inputSchema=params)
    server._tools = [tool('old', {'type': 'object', 'properties': {}})]
    server._config = {}
    server._ready.set()
    server._registered_tool_names = _register_server_tools(server.name, server, {})
    name = server._registered_tool_names[0]
    other = SimpleNamespace(_refresh_tools=AsyncMock())
    registry.register(name='mcp__other__lookup', toolset='mcp-other', schema={'name': 'mcp__other__lookup'},
                      handler=lambda _: '')
    other_schema = registry.get_schema('mcp__other__lookup')
    from tools.mcp_tool_scope import _resolve_server_key
    monkeypatch.setattr(core, '_servers', {_resolve_server_key(server.name): server,
                                         _resolve_server_key('other'): other})
    monkeypatch.setattr(harso, '_run_on_mcp_loop', lambda factory, **kw: asyncio.run(factory()))
    original = [{'type': 'function', 'function': deepcopy(registry.get_schema(name))}]
    agent = SimpleNamespace(tools=original, valid_tool_names={name}, enabled_toolsets=['mcp-weave-connectors'],
                            disabled_toolsets=None, _tool_snapshot_generation=-1)
    server.session = SimpleNamespace(list_tools=AsyncMock(return_value=SimpleNamespace(
        tools=[tool(description, schema or {'type': 'object', 'properties': {}})])))
    assert harso.refresh_mcp_server_tools(server.name) == [name]
    assert agent.tools is original
    assert registry.get_schema(name)['description'] == description
    assert registry.get_schema('mcp__other__lookup') is other_schema
    other._refresh_tools.assert_not_called()
    server.session.list_tools.assert_awaited_once()
    def selected_defs(**kwargs):
        assert kwargs['enabled_toolsets'] == ['mcp-weave-connectors']
        return [{'type': 'function', 'function': registry.get_schema(name)}]
    monkeypatch.setattr(model_tools, 'get_tool_definitions', selected_defs)
    monkeypatch.setattr(discovery, 'has_registered_mcp_tools', lambda: True)
    monkeypatch.setattr(maintenance, '_reinject_post_build_tools', lambda *a: set())
    _refresh_mcp_tools_between_turns(agent)
    assert agent.tools is not original
    assert agent.tools[0]['function'] == registry.get_schema(name)
    assert agent.valid_tool_names == {name}


def test_forwarded_tool_start_uses_final_arguments(monkeypatch):
    from agent import tool_executor as executor, relay_tools
    from hermes_cli import middleware
    begin = Mock()
    monkeypatch.setattr(executor, '_begin_tool_execution', begin)
    monkeypatch.setattr(relay_tools, 'execute', lambda name, args, call, **kw: (call(args), args))
    monkeypatch.setattr(middleware, 'apply_tool_request_middleware',
                        lambda *a, **kw: SimpleNamespace(payload=a[1], trace=[]))
    def forwarded(name, args, next_call, **context):
        context['on_execution_started']({'target': 'final'})
        context['on_execution_started']({'target': 'duplicate'})
        return 'forwarded'
    monkeypatch.setattr(middleware, 'run_tool_execution_middleware', forwarded)
    agent = SimpleNamespace(session_id='weave-test')
    execute = Mock()
    state = executor._run_agent_tool_execution_middleware(agent, function_name='mcp__hub__send',
        function_args={'target': 'original'}, effective_task_id='task', tool_call_id='call', execute=execute)
    assert state.result == 'forwarded'
    execute.assert_not_called()
    assert begin.call_args.args[1].args == {'target': 'final'}
    assert begin.call_count == 1
    assert begin.call_args.args[1].call_id == 'call'


def test_always_eager_pins_exact_names_only():
    from tools import tool_search as ts
    config = ts.ToolSearchConfig.from_raw({'always_eager': ['pinned'], 'defer': ['pinned', 'other']})
    defs = [{'function': {'name': name}} for name in ['pinned', 'other']]
    visible, deferred = ts.classify_tools(defs, config.effective_defer_tools, config.always_eager)
    assert visible == [defs[0]] and deferred == [defs[1]]
    for invalid in ['pinned', ['pinned', 'pinned'], ['pinned', 1], list(map(str, range(65)))]:
        assert not ts.ToolSearchConfig.from_raw({'always_eager': invalid}).always_eager
