"""Config.yaml reconciliation through real, credential-free MCP stdio tasks."""

import sys
import asyncio
import concurrent.futures
import time
from pathlib import Path
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
        "_server_connecting_since", "_server_connect_tasks",
    ):
        monkeypatch.setattr(mcp, name, {})
    for name in (
        "_session_mcp_managed", "_session_mcp_releasing", "_server_connecting",
        "_parallel_safe_servers", "_orphan_stdio_pids", "_mcp_reconciling",
    ):
        monkeypatch.setattr(mcp, name, set())
    monkeypatch.setattr(mcp, "_mcp_loop", None)
    monkeypatch.setattr(mcp, "_mcp_thread", None)
    script = tmp_path / "fake_mcp.py"
    script.write_text(_STDIO_SERVER)

    def descriptor(label="A", **overrides):
        return {"command": sys.executable, "args": [str(script), label],
                "connect_timeout": 5, "lazy": False, **overrides}

    def publish(servers, **settings):
        (get_hermes_home() / "config.yaml").write_text(
            yaml.safe_dump({"mcp_servers": servers, "mcp": settings})
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

    outcomes = mcp.reconcile_mcp_servers({"fixture": changed, "sibling": sibling_cfg})
    assert outcomes == {"fixture": "replaced", "sibling": "unchanged"}
    names = [n for n in runtime.registry.get_all_tool_names() if n.startswith("mcp__")]

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

    assert mcp.reconcile_mcp_servers({}) == {"fixture": "removed"}

    assert "fixture" not in mcp._servers
    assert old._config is old_config
    assert old.session is None
    assert old._task.done()
    assert not (_names() & set(runtime.registry.get_all_tool_names()))
    assert not old._registered_tool_names
    assert runtime.registry._generation > generation
    unchanged_generation = runtime.registry._generation
    assert mcp.reconcile_mcp_servers({}) == {}
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

    assert mcp.reconcile_mcp_servers({"fixture": runtime.descriptor("B")}) == {"fixture": "refused_owned"}

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

    assert mcp.reconcile_mcp_servers({"fixture": runtime.descriptor("B")}) == {"fixture": "refused_connecting"}

    assert mcp._servers["fixture"] is old
    assert old._config is old_config
    assert set(old._registered_tool_names) == _names()
    assert runtime.registry._generation == generation
    assert "fixture" in mcp._server_connecting
    mcp._server_connecting.remove("fixture")
    assert mcp.reconcile_mcp_servers({"fixture": runtime.descriptor("B")}) == {"fixture": "replaced"}
    assert mcp._servers["fixture"] is not old
    assert mcp._servers["fixture"]._config == runtime.descriptor("B")
    assert runtime.registry._generation > generation


def test_unchanged_config_preserves_task_and_generation(runtime):
    old = _start(runtime)
    old_config = old._config
    generation = runtime.registry._generation
    runtime.publish({"fixture": runtime.descriptor()})

    assert mcp.reconcile_mcp_servers({"fixture": runtime.descriptor()}) == {"fixture": "unchanged"}

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

    assert mcp.reconcile_mcp_servers({"fixture": runtime.descriptor(enabled=False)}) == {"fixture": "removed"}

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

    assert mcp.reconcile_mcp_servers({"fixture": changed}) == {"fixture": "connect_failed"}

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


def test_acquired_descriptor_must_not_change(runtime):
    old = _start(runtime)
    mcp.acquire_session_mcp_servers("borrower", {"fixture": old._config})
    assert "fixture" not in mcp._session_mcp_managed
    generation = runtime.registry._generation
    assert mcp.reconcile_mcp_servers({"fixture": runtime.descriptor("B")}) == {"fixture": "refused_owned"}
    assert mcp._servers["fixture"] is old
    assert runtime.registry._generation == generation
    assert "A:beta" in mcp._make_tool_handler("fixture", "beta", 5)({})
    mcp.release_session_mcp_servers("borrower")


def test_active_rpc_drains_without_abort(runtime):
    cfg = runtime.descriptor()
    script = Path(cfg["args"][0])
    marker = script.parent / "active-rpc"
    release = script.parent / "release-rpc"
    script.write_text(_STDIO_SERVER.replace(
        "elif method == 'tools/call':",
        "elif method == 'tools/call':\n        from pathlib import Path\n        import time\n        Path(" + repr(str(marker)) + ").touch()\n        while not Path(" + repr(str(release)) + ").exists(): time.sleep(.01)"))
    old = _start(runtime)
    changed = {"fixture": runtime.descriptor("B")}
    with concurrent.futures.ThreadPoolExecutor() as pool:
        call = pool.submit(mcp._make_tool_handler("fixture", "beta", 10), {})
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert marker.exists()
            assert mcp.reconcile_mcp_servers(changed, drain_seconds=.1) == {"fixture": "refused_busy"}
            assert mcp._servers["fixture"] is old
        finally:
            release.touch()
        assert "A:beta" in call.result(timeout=10)
    assert mcp.reconcile_mcp_servers(changed) == {"fixture": "replaced"}
    assert mcp._servers["fixture"] is not old


def test_root_authority_and_profile_discovery_are_separate(runtime):
    from gateway.run import _profile_runtime_scope
    root = {"a_only": runtime.descriptor("A"), "b_only": runtime.descriptor("B")}
    runtime.publish(root)
    mcp.discover_mcp_tools()
    before = dict(mcp._servers)
    generation = runtime.registry._generation
    assert mcp.reconcile_mcp_servers(root) == {"a_only": "unchanged", "b_only": "unchanged"}
    assert runtime.registry._generation == generation
    for name in root:
        home = get_hermes_home() / "profiles" / name
        home.mkdir(parents=True)
        (home / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": {name: root[name]}}))
        with _profile_runtime_scope(home):
            mcp.discover_mcp_tools()
        assert all(mcp._servers.get(n) is task for n, task in before.items())
    # Profile entry can register unrelated plugins; MCP identities/tools stay.
    assert all(task.session is not None for task in before.values())
    assert _names("a_only") | _names("b_only") <= set(runtime.registry.get_all_tool_names())


@pytest.mark.parametrize("age,live,expected", [(20, False, "replaced"), (0, False, "refused_connecting"), (20, True, "refused_connecting")])
def test_connecting_marker_age_and_live_task(runtime, age, live, expected):
    mcp._server_connecting.add("fixture")
    mcp._server_connecting_since["fixture"] = time.monotonic() - age
    mcp._ensure_mcp_loop()
    async def hold():
        await asyncio.Event().wait()
    async def create():
        return asyncio.create_task(hold())
    task = mcp._run_on_mcp_loop(create) if live else None
    if task:
        mcp._server_connect_tasks["fixture"] = task
    try:
        assert mcp.reconcile_mcp_servers({"fixture": runtime.descriptor()}, drain_seconds=10) == {"fixture": expected}
        if expected == "replaced":
            assert "fixture" not in mcp._server_connecting
            assert mcp._servers["fixture"]._config == runtime.descriptor()
        else:
            assert "fixture" in mcp._server_connecting
            assert "fixture" not in mcp._servers
    finally:
        if task:
            mcp._mcp_loop.call_soon_threadsafe(task.cancel)


@pytest.mark.parametrize("budget,expected", [(5, ["replaced"] * 3), (.3, ["replaced", "refused_busy", "refused_busy"])])
def test_shutdown_budget_leaves_remainder_intact(runtime, monkeypatch, budget, expected):
    original = {name: runtime.descriptor("A") for name in ("a", "b", "c")}
    # Budget test injects transport latency, not OS-dependent process startup.
    # The Probe-2 tests above separately exercise real stdio replacement.
    async def connect(name, config):
        server = mcp.MCPServerTask(name)
        server._config = config
        server.session = object()
        mcp._servers[name] = server
        return []
    async def slow_shutdown(self):
        await asyncio.sleep(.2)
        self._shutdown_event.set()
        self.session = None
        self._deregister_tools()
    monkeypatch.setattr(mcp, "_discover_and_register_server", connect)
    monkeypatch.setattr(mcp.MCPServerTask, "shutdown", slow_shutdown)
    mcp._ensure_mcp_loop()
    for name, cfg in original.items():
        mcp._run_on_mcp_loop(lambda: connect(name, cfg))
    before = dict(mcp._servers)
    # A server needs its configured full shutdown allowance before admission.
    runtime.publish(original, reconcile_shutdown_seconds=.25, reconcile_budget_seconds=budget)
    changed = {name: runtime.descriptor("B") for name in original}
    outcomes = mcp.reconcile_mcp_servers(changed, drain_seconds=0)
    assert outcomes == dict(zip(original, expected))
    for name, outcome in outcomes.items():
        if outcome == "replaced":
            assert mcp._servers[name] is not before[name]
            assert before[name].session is None
            assert mcp._servers[name]._config == changed[name]
        else:
            assert mcp._servers[name] is before[name]
            assert before[name].session is not None
            assert not before[name]._shutdown_event.is_set()
    assert not mcp._mcp_reconciling


def test_reconcile_config_loaded_once_and_hot(runtime, monkeypatch):
    import hermes_cli.config as config_module
    old = _start(runtime)
    real_load = config_module.load_config
    calls = []
    def load(*args, **kwargs):
        calls.append(True)
        return real_load(*args, **kwargs)
    monkeypatch.setattr(config_module, "load_config", load)
    runtime.publish({}, reconcile_budget_seconds=0)
    assert mcp.reconcile_mcp_servers({}) == {"fixture": "refused_busy"}
    assert len(calls) == 1
    assert mcp._servers["fixture"] is old
    runtime.publish({}, reconcile_budget_seconds=60)
    calls.clear()
    assert mcp.reconcile_mcp_servers({}) == {"fixture": "removed"}
    assert len(calls) == 1


@pytest.mark.parametrize("operation", ["resources/list", "resources/read", "prompts/list", "prompts/get"])
def test_resource_and_prompt_operations_are_drained(runtime, operation):
    old = _start(runtime)
    entered = asyncio.Event()
    release = asyncio.Event()
    async def held(*args, **kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(resources=[], contents=[], prompts=[], messages=[], next_cursor=None)
    field = {"resources/list": "list_resources", "resources/read": "read_resource", "prompts/list": "list_prompts", "prompts/get": "get_prompt"}[operation]
    setattr(old.session, field, held)
    handler = {"resources/list": mcp._make_list_resources_handler, "resources/read": mcp._make_read_resource_handler,
               "prompts/list": mcp._make_list_prompts_handler, "prompts/get": mcp._make_get_prompt_handler}[operation]("fixture", 10)
    runtime.publish({}, reconcile_drain_seconds=.01)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        call = pool.submit(handler, {"uri": "test://resource", "name": "test"})
        mcp._run_on_mcp_loop(entered.wait, timeout=5)
        try:
            assert mcp.reconcile_mcp_servers({}) == {"fixture": "refused_busy"}
            assert mcp._servers["fixture"] is old
        finally:
            mcp._mcp_loop.call_soon_threadsafe(release.set)
        assert '"error"' not in call.result(timeout=10)


def test_reservation_fences_acquire_and_late_rpc(runtime, monkeypatch):
    import threading
    old = _start(runtime)
    entered = threading.Event()
    release = threading.Event()
    real = mcp._teardown_mcp_server
    async def held(name, server):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(.01)
        await real(name, server)
    monkeypatch.setattr(mcp, "_teardown_mcp_server", held)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        reconcile = pool.submit(mcp.reconcile_mcp_servers, {"fixture": runtime.descriptor("B")})
        try:
            assert entered.wait(5)
            with pytest.raises(RuntimeError):
                mcp.acquire_session_mcp_servers("late-owner", {"fixture": old._config})
            result = mcp._make_tool_handler("fixture", "beta", 5)({})
            assert "being reconciled" in result
            assert not old._inflight_tasks
        finally:
            release.set()
        assert reconcile.result(timeout=10) == {"fixture": "replaced"}
    assert not mcp._mcp_reconciling


def test_shutdown_timeout_retains_fence_until_cleanup(runtime, monkeypatch):
    import threading
    old = _start(runtime)
    release = threading.Event()
    real_shutdown = mcp.MCPServerTask.shutdown
    async def held(self):
        while not release.is_set():
            await asyncio.sleep(.01)
        await real_shutdown(self)
    monkeypatch.setattr(mcp.MCPServerTask, "shutdown", held)
    runtime.publish({}, reconcile_shutdown_seconds=.02, reconcile_budget_seconds=.1)
    try:
        started = time.monotonic()
        assert mcp.reconcile_mcp_servers({}) == {"fixture": "connect_failed"}
        assert time.monotonic() - started < 2
        assert "fixture" in mcp._server_connect_errors
        assert "fixture" in mcp._mcp_reconciling
        assert mcp._servers["fixture"] is old
        assert mcp.reconcile_mcp_servers({}) == {"fixture": "refused_busy"}
    finally:
        release.set()
    async def settled():
        while "fixture" in mcp._mcp_reconciling:
            await asyncio.sleep(.01)
    mcp._run_on_mcp_loop(settled, timeout=5)
    assert "fixture" not in mcp._servers
    assert old.session is None



def test_connection_budget_cancels_without_late_publication(runtime):
    cfg = runtime.descriptor()
    script = Path(cfg["args"][0])
    script.write_text(_STDIO_SERVER.replace(
        "if method == 'initialize':",
        "if method == 'initialize':\n        import time\n        time.sleep(5)"))
    runtime.publish({}, reconcile_shutdown_seconds=.01, reconcile_budget_seconds=.15)
    started = time.monotonic()
    assert mcp.reconcile_mcp_servers({"fixture": cfg}) == {"fixture": "connect_failed"}
    assert time.monotonic() - started < 2
    async def settled():
        while "fixture" in mcp._mcp_reconciling:
            await asyncio.sleep(.01)
    mcp._run_on_mcp_loop(settled, timeout=10)
    assert "fixture" not in mcp._servers
    assert "fixture" not in mcp._server_connecting
    assert "fixture" in mcp._server_connect_errors
    assert not (_names() & set(runtime.registry.get_all_tool_names()))


@pytest.mark.parametrize("servers", [None, [], {"bad": 1}, {"": {}}])
def test_bad_authority_is_rejected_without_teardown(runtime, servers):
    old = _start(runtime)
    with pytest.raises(ValueError):
        mcp.reconcile_mcp_servers(servers)
    assert mcp._servers["fixture"] is old
    assert old.session is not None
