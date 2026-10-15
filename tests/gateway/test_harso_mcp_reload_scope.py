from types import SimpleNamespace
from unittest.mock import Mock
from gateway.platforms import api_server_harso_mcp as mcp


def test_full_reload_is_owner_scoped_and_never_rewrites_agents(monkeypatch):
    from tools import mcp_tool as core, mcp_tool_lifecycle as lifecycle, mcp_tool_discovery as discovery
    selected, other = object(), object()
    monkeypatch.setattr(core, '_servers', {'weave-connectors': selected, ('work', 'weave-connectors'): other})
    monkeypatch.setattr(core, '_mcp_registry_scope', lambda: None)
    shutdown, discover = Mock(), Mock(return_value=['one'])
    monkeypatch.setattr(lifecycle, 'shutdown_mcp_servers', shutdown)
    monkeypatch.setattr(discovery, 'discover_mcp_tools', discover)
    assert mcp.reload_tools(None)['tools'] == 1
    shutdown.assert_called_once_with(scope=None, names={'weave-connectors'})
    discover.assert_called_once_with()
    assert core._servers[('work', 'weave-connectors')] is other
