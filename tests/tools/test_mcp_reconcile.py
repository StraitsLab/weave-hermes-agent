"""Config.yaml reconciliation through real, credential-free MCP stdio tasks."""

import sys
from types import SimpleNamespace

import pytest
import yaml

from hermes_constants import get_hermes_home
from tools import mcp_tool as mcp
from tools.registry import ToolRegistry


# Same JSON-RPC shape as the B1 probe, embedded so this suite is self-contained.
_STDIO_SERVER = """
import json, sys
label = sys.argv[1]
for line in sys.stdin:
    msg = json.loads(line)
    if 'id' not in msg:
        continue
    method = msg.get('method')
    if method == 'initialize':
        result = {'protocolVersion': msg['params']['protocolVersion'],
                  'capabilities': {'tools': {}},
                  'serverInfo': {'name': 'reconcile-' + label, 'version': '1'}}
    elif method == 'tools/list':
        result = {'tools': [
            {'name': name, 'description': label + ' ' + name,
             'inputSchema': {'type': 'object', 'properties': {}}}
            for name in ['alpha', 'beta']]}
    elif method == 'tools/call':
        result = {'content': [{'type': 'text',
                              'text': label + ':' + msg['params']['name']}],
                  'isError': False}
    elif method == 'ping':
        result = {}
    elif method in ['resources/list', 'resources/templates/list', 'prompts/list']:
        result = {'resources/list': {'resources': []},
                  'resources/templates/list': {'resourceTemplates': []},
                  'prompts/list': {'prompts': []}}[method]
    else:
        print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'],
                          'error': {'code': -32601, 'message': 'Unknown method'}}),
              flush=True)
        continue
    print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}), flush=True)
"""


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    import tools.registry as registry_module

    monkeypatch.setenv("HOME", str(tmp_path))
    registry = ToolRegistry()
    monkeypatch.setattr(registry_module, "registry", registry)
    for name in (
        "_servers", "_server_connect_errors", "_session_mcp_fingerprints",
        "_session_mcp_owners", "_lazy_server_configs", "_lazy_server_fingerprints",
        "_lazy_server_tool_names", "_server_connect_retry_after",
        "_server_connect_failures", "_mcp_tool_server_names", "_stdio_pids",
        "_stdio_pgids", "_orphan_stdio_pid_servers", "_server_trust_levels",
        "_tool_read_only_hints", "_server_error_counts", "_server_breaker_opened_at",
    ):
        monkeypatch.setattr(mcp, name, {})
    for name in (
        "_session_mcp_managed", "_session_mcp_releasing", "_server_connecting",
        "_parallel_safe_servers", "_orphan_stdio_pids",
    ):
        monkeypatch.setattr(mcp, name, set())
    monkeypatch.setattr(mcp, "_mcp_loop", None)
    monkeypatch.setattr(mcp, "_mcp_thread", None)
    script = tmp_path / "fake_mcp.py"
    script.write_text(_STDIO_SERVER)

    def descriptor(label="A", **overrides):
        return {"command": sys.executable, "args": [str(script), label],
                "connect_timeout": 5, "lazy": False, **overrides}

    def publish(servers):
        (get_hermes_home() / "config.yaml").write_text(
            yaml.safe_dump({"mcp_servers": servers})
        )

    yield SimpleNamespace(registry=registry, descriptor=descriptor, publish=publish)
    mcp.shutdown_mcp_servers()


def _names(name="fixture", tools=("alpha", "beta")):
    return {f"mcp__{name}__{tool}" for tool in tools}


def _start(runtime, **extra):
    cfg = runtime.descriptor()
    runtime.publish({"fixture": cfg, **extra})
    mcp.discover_mcp_tools()
    server = mcp._servers["fixture"]
    assert server._config == cfg
    assert set(server._registered_tool_names) == _names()
    return server


def test_changed_descriptor_reconnects_only_that_server(runtime):
    sibling_cfg = runtime.descriptor("SIBLING")
    old = _start(runtime, sibling=sibling_cfg)
    sibling = mcp._servers["sibling"]
    old_config = old._config
    generation = runtime.registry._generation
    changed = runtime.descriptor("B", tools={"include": ["beta"]})
    runtime.publish({"fixture": changed, "sibling": sibling_cfg})

    names = mcp.discover_mcp_tools()

    new = mcp._servers["fixture"]
    assert new is not old
    assert new._config == changed
    assert new._config is not old_config
    assert old.session is None
    assert old._task.done()
    assert set(new._registered_tool_names) == _names(tools=("beta",))
    assert set(names) == _names(tools=("beta",)) | _names("sibling")
    assert {name for name in runtime.registry.get_all_tool_names()
            if name.startswith("mcp__")} == set(names)
    assert runtime.registry._generation > generation
    assert mcp._servers["sibling"] is sibling
    assert sibling._config == sibling_cfg
    assert sibling.session is not None
    result = mcp._run_on_mcp_loop(lambda: new.session.call_tool("beta", {}), timeout=5)
    assert result.content[0].text == "B:beta"


def test_removed_server_is_unregistered_even_when_config_is_empty(runtime):
    old = _start(runtime)
    old_config = old._config
    generation = runtime.registry._generation
    runtime.publish({})

    assert mcp.discover_mcp_tools() == []

    assert "fixture" not in mcp._servers
    assert old._config is old_config
    assert old.session is None
    assert old._task.done()
    assert not (_names() & set(runtime.registry.get_all_tool_names()))
    assert not old._registered_tool_names
    assert runtime.registry._generation > generation
    unchanged_generation = runtime.registry._generation
    assert mcp.discover_mcp_tools() == []
    assert runtime.registry._generation == unchanged_generation


def test_session_managed_descriptor_is_untouched(runtime):
    cfg = runtime.descriptor()
    runtime.publish({"fixture": cfg})
    mcp.acquire_session_mcp_servers("owner", {"fixture": cfg})
    mcp.discover_mcp_tools()
    old = mcp._servers["fixture"]
    old_config = old._config
    generation = runtime.registry._generation
    runtime.publish({"fixture": runtime.descriptor("B")})

    assert set(mcp.discover_mcp_tools()) == _names()

    assert mcp._servers["fixture"] is old
    assert old._config is old_config
    assert old._config == cfg
    assert set(old._registered_tool_names) == _names()
    assert runtime.registry._generation == generation
    assert mcp._session_mcp_owners["fixture"] == {"owner"}
    mcp.release_session_mcp_servers("owner")
    assert "fixture" not in mcp._servers
    assert not (_names() & set(runtime.registry.get_all_tool_names()))


def test_connecting_server_is_untouched_this_pass(runtime):
    old = _start(runtime)
    old_config = old._config
    generation = runtime.registry._generation
    runtime.publish({"fixture": runtime.descriptor("B")})
    mcp._server_connecting.add("fixture")

    assert set(mcp.discover_mcp_tools()) == _names()

    assert mcp._servers["fixture"] is old
    assert old._config is old_config
    assert set(old._registered_tool_names) == _names()
    assert runtime.registry._generation == generation
    assert "fixture" in mcp._server_connecting
    mcp._server_connecting.remove("fixture")
    mcp.discover_mcp_tools()
    assert mcp._servers["fixture"] is not old
    assert mcp._servers["fixture"]._config == runtime.descriptor("B")
    assert runtime.registry._generation > generation


def test_unchanged_config_preserves_task_and_generation(runtime):
    old = _start(runtime)
    old_config = old._config
    generation = runtime.registry._generation
    runtime.publish({"fixture": runtime.descriptor()})

    assert set(mcp.discover_mcp_tools()) == _names()

    assert mcp._servers["fixture"] is old
    assert old._config is old_config
    assert set(old._registered_tool_names) == _names()
    assert _names() <= set(runtime.registry.get_all_tool_names())
    assert runtime.registry._generation == generation


def test_disabled_server_is_disconnected_and_unregistered(runtime):
    old = _start(runtime)
    old_config = old._config
    generation = runtime.registry._generation
    runtime.publish({"fixture": runtime.descriptor(enabled=False)})

    assert mcp.discover_mcp_tools() == []

    assert "fixture" not in mcp._servers
    assert old._config is old_config
    assert old.session is None
    assert old._task.done()
    assert not old._registered_tool_names
    assert not (_names() & set(runtime.registry.get_all_tool_names()))
    assert runtime.registry._generation > generation
    assert "fixture" not in mcp._server_connect_errors
    assert "fixture" not in mcp._server_connecting
    assert "fixture" not in mcp._parallel_safe_servers


def test_reconnect_failure_drops_old_connection_and_tools(runtime):
    old = _start(runtime)
    old_config = old._config
    generation = runtime.registry._generation
    changed = runtime.descriptor()
    changed["args"] = ["-c", "raise SystemExit(1)"]
    runtime.publish({"fixture": changed})

    assert mcp.discover_mcp_tools() == []

    assert "fixture" not in mcp._servers
    assert "fixture" in mcp._server_connect_errors
    assert mcp._server_connect_errors["fixture"]
    assert "fixture" not in mcp._server_connecting
    assert old._config is old_config
    assert old.session is None
    assert old._task.done()
    assert not old._registered_tool_names
    assert not (_names() & set(runtime.registry.get_all_tool_names()))
    assert runtime.registry._generation > generation
