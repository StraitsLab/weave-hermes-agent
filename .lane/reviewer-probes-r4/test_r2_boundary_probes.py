"""Independent R2 probes; F4 latch consistency does not authorize OFF content."""
import json
import types
import pytest
from test_harso_copilot import _switch, _provider, _capture, _manager, _tool_agent, _DELIVERY
from agent.memory_manager import inject_memory_provider_tools

@pytest.mark.parametrize('action,args', [('brief', {}), ('search', {'query':'tea'}), ('open', {'ref':'m7'})])
@pytest.mark.parametrize('enabled', [True, False])
def test_f4_latched_route_obeys_live_content_switch(monkeypatch, action, args, enabled):
    _switch(True)
    p = _provider(monkeypatch)
    m = _manager(p)
    a = _tool_agent(m)
    inject_memory_provider_tools(a)
    _switch(enabled)
    seen = _capture(monkeypatch, {'/memory-tool': {'items':[{'text':'OFF_CONTENT_SENTINEL'}]}})
    names = {t['function']['name'] for t in a.tools}
    assert 'harso_memory' in names and m.has_tool('harso_memory')
    out = m.handle_tool_call('harso_memory', {'action':action, **args})
    print(f'action={action} live_on={enabled} advertised=True routed=True posts={len(seen)} rendered={"OFF_CONTENT_SENTINEL" in out}')
    if enabled:
        assert len(seen) == 1 and 'OFF_CONTENT_SENTINEL' in out
    else:
        assert not seen and 'OFF_CONTENT_SENTINEL' not in out, 'OFF must keep dispatch routable but must not fetch or render copilot content'

@pytest.mark.parametrize('path', ['tool', 'late', 'prefetch'])
def test_f4_switch_off_during_fetch_withholds_delivery(monkeypatch, path):
    _switch(True)
    p = _provider(monkeypatch)
    m = _manager(p)
    a = _tool_agent(m)
    inject_memory_provider_tools(a)
    def post(*args, **kwargs):
        _switch(False)
        return {'recall_status':'ok', 'delivery':_DELIVERY, 'items':[{'text':'INFLIGHT_SENTINEL','citation':'[harso: e1]'}]}
    monkeypatch.setattr(p, '_post', post)
    if path == 'tool':
        out = m.handle_tool_call('harso_memory', {'action':'brief'})
    elif path == 'late':
        out = p.fetch_mid_turn_delivery(visible_seqs=[])
    else:
        out = p.prefetch('What do you remember?')
    rendered = 'Harso memory delivery' in out or 'INFLIGHT_SENTINEL' in out
    print(f'path={path} live_on={p.copilot_active()} rendered_after_off={rendered}')
    assert not rendered, 'content response completed after OFF must not render copilot delivery'

@pytest.mark.parametrize('start_on', [False, True])
def test_f4_acp_listing_does_not_move_either_live_route(monkeypatch, start_on):
    from acp_adapter.server import HermesACPAgent
    import model_tools
    _switch(start_on)
    p = _provider(monkeypatch)
    m = _manager(p)
    a = _tool_agent(m)
    a.enabled_toolsets = ['hermes-acp']
    inject_memory_provider_tools(a)
    before_tools = list(a.tools)
    before_routes = dict(m._tool_to_provider)
    _switch(not start_on)
    monkeypatch.setattr(model_tools, 'get_tool_definitions', lambda **kw: [
        {'type':'function', 'function':{'name':n,'description':'','parameters':{}}}
        for n in ['session_search','memory']])
    result = HermesACPAgent._cmd_tools(HermesACPAgent.__new__(HermesACPAgent), '', types.SimpleNamespace(agent=a))
    assert a.tools == before_tools and m._tool_to_provider == before_routes
    assert m.has_tool('harso_memory') == start_on
    print(f'acp_listing_start_on={start_on} live_surface_and_routing_unchanged=True')

@pytest.mark.parametrize('start_on', [False, True])
def test_f4_mcp_refresh_maintains_latched_consistency(monkeypatch, start_on):
    from tools.mcp_tool import refresh_agent_mcp_tools
    import model_tools
    _switch(start_on)
    p = _provider(monkeypatch)
    m = _manager(p)
    a = _tool_agent(m)
    inject_memory_provider_tools(a)
    _switch(not start_on)
    monkeypatch.setattr(model_tools, 'get_tool_definitions', lambda **kw: [
        {'type':'function', 'function':{'name':n,'description':'','parameters':{}}}
        for n in ['session_search','memory','mcp_new']])
    refresh_agent_mcp_tools(a)
    names = {t['function']['name'] for t in a.tools}
    assert ('harso_memory' in names) == m.has_tool('harso_memory') == start_on
    assert ('session_search' in names) != start_on
    print(f'mcp_refresh_start_on={start_on} surface_routing_consistent=True')
