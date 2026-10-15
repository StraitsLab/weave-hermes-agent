"""Harso's explicit scoped refresh and refusal contracts, ported from the cell fork."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tools import mcp_tool as core
from tools import mcp_tool_handlers as handlers


def test_application_refusals_do_not_open_transport_breaker(monkeypatch):
    bumps, resets = [], []
    monkeypatch.setattr(core, '_bump_server_error', lambda *a, **k: bumps.append(a))
    monkeypatch.setattr(core, '_reset_server_error', resets.append)
    for _ in range(5):
        assert handlers._record_call_outcome('hub', '{"error":"wrong account"}')
    assert not bumps
    assert resets == ['hub'] * 5


def test_read_only_sdk_snake_case_annotation():
    from tools.mcp_tool_registration import _annotation_read_only_hint
    assert _annotation_read_only_hint(SimpleNamespace(annotations=SimpleNamespace(read_only_hint=True)))
    assert not _annotation_read_only_hint(SimpleNamespace(annotations=SimpleNamespace(read_only_hint='true')))


def test_scoped_refresh_never_discovers_or_touches_other_server(monkeypatch):
    from tools import mcp_tool_harso as harso
    from tools.mcp_tool_scope import _resolve_server_key
    selected = SimpleNamespace(_refresh_tools=AsyncMock(), _registered_tool_names=['new'])
    other = SimpleNamespace(_refresh_tools=AsyncMock(), _registered_tool_names=['old'])
    monkeypatch.setattr(core, '_servers', {_resolve_server_key('weave-connectors'): selected,
                                         _resolve_server_key('other'): other})
    monkeypatch.setattr(harso, '_run_on_mcp_loop', lambda factory, **kw: asyncio.run(factory()))
    assert harso.refresh_mcp_server_tools('weave-connectors') == ['new']
    selected._refresh_tools.assert_awaited_once_with(require_ready=True)
    other._refresh_tools.assert_not_called()


def test_refresh_failure_does_not_report_success(monkeypatch):
    from tools import mcp_tool_harso as harso
    monkeypatch.setattr(core, '_servers', {})
    monkeypatch.setattr(harso, '_run_on_mcp_loop', lambda factory, **kw: asyncio.run(factory()))
    with pytest.raises(RuntimeError, match='not connected'):
        harso.refresh_mcp_server_tools('weave-connectors')


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['missing', 'replaced', 'list_error'])
async def test_explicit_refresh_keeps_old_schema_on_failure(monkeypatch, failure):
    from tools.mcp_tool import MCPServerTask
    from tools.registry import ToolRegistry
    registry = ToolRegistry()
    monkeypatch.setattr('tools.registry.registry', registry)
    server = MCPServerTask('weave-connectors')
    name = 'mcp__weave_connectors__old'
    registry.register(name=name, toolset='mcp-weave-connectors', schema={'name': name}, handler=lambda _: '')
    original = registry.get_schema(name)
    server._registered_tool_names = [name]
    server._ready.set()
    async def list_tools():
        if failure == 'replaced':
            server.session = None
        if failure == 'list_error':
            raise RuntimeError('list failed')
        return SimpleNamespace(tools=[])
    server.session = None if failure == 'missing' else SimpleNamespace(list_tools=list_tools)
    with pytest.raises(RuntimeError):
        await server._refresh_tools(require_ready=True)
    assert registry.get_schema(name) is original
    assert server._registered_tool_names == [name]
