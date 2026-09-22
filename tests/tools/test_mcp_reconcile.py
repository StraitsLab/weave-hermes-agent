"""Config.yaml reconciliation through real, credential-free MCP stdio tasks."""

import sys
import asyncio
import concurrent.futures
import threading
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
        "_mcp_descriptor_generation",
    ):
        monkeypatch.setattr(mcp, name, {})
    for name in (
        "_session_mcp_managed", "_session_mcp_releasing", "_server_connecting",
        "_parallel_safe_servers", "_orphan_stdio_pids", "_mcp_reconciling",
    ):
        monkeypatch.setattr(mcp, name, set())
    monkeypatch.setattr(mcp, "_mcp_loop", None)
    monkeypatch.setattr(mcp, "_mcp_thread", None)
    # Bundled plugin discovery (a2a, spotify, ...) registers unrelated plugin
    # tools into whichever ``tools.registry.registry`` is current whenever
    # ``discover_mcp_tools`` loads config through ``_load_mcp_config``. The
    # gate reviewer's acceptance assertions compare the WHOLE registry
    # (``names == _names(...)``), so this fixture isolates the registry from
    # plugin registration. MCP discovery, lazy cache publication, and
    # reconcile are exercised unchanged.
    import hermes_cli.plugins as plugins_module
    monkeypatch.setattr(plugins_module, "discover_plugins", lambda *args, **kwargs: None)
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


# ---------------------------------------------------------------------------
# WEV-1795 v3 — gate-review P1: lazy-cache publication vs the reconcile fence.
#
# Ported from .lane/review-gate/test_lazy_safety.py (assertions verbatim) and
# .lane/review-gate/test_gate_hunts.py (the two defect probes, inverted). The
# gate reviewer's callback barrier paused the native cache publisher at its
# entry; the drivers below keep that seam and additionally hold reconcile's
# transition open (post-teardown) so the resumed publisher deterministically
# runs inside the fenced window the publisher-side check must reject.
# ---------------------------------------------------------------------------

def _hold_cache_publisher(monkeypatch):
    """Pause at the entry of the native cache publisher (gate barrier)."""
    entered = threading.Event()
    release = threading.Event()
    real = mcp._register_from_cache_sync

    def held(name, config, entry):
        entered.set()
        assert release.wait(10)
        return real(name, config, entry)

    monkeypatch.setattr(mcp, "_register_from_cache_sync", held)
    return entered, release


def _gated_reconcile_teardown(monkeypatch):
    """Hold reconcile's fence after its teardown cleanup (transition seam)."""
    done = threading.Event()
    go = threading.Event()
    real_teardown = mcp._teardown_mcp_server

    async def gated(name, server):
        await real_teardown(name, server)
        done.set()
        while not go.is_set():
            await asyncio.sleep(0.005)

    monkeypatch.setattr(mcp, "_teardown_mcp_server", gated)
    return done, go


@pytest.mark.parametrize("entry", ["register", "discovery"])
@pytest.mark.parametrize("replace", [False, True])
def test_reconcile_does_not_ack_then_accept_stale_lazy_publication(runtime, monkeypatch, replace, entry):
    """Port of .lane/review-gate/test_lazy_safety.py; assertions verbatim.

    Driver adaptation (assertions unchanged): the paused cache publisher is
    resumed WHILE the authoritative reconcile transition holds its
    ``_mcp_reconciling`` fence, which is exactly the window the gate reviewer
    found the publisher could exploit. The marker is aged past the drain
    window first so the transition proceeds instead of answering a truthful
    ``refused_connecting`` forever.
    """
    from tools.mcp_schema_cache import config_fingerprint, write_cache_entry
    a = runtime.descriptor("A", lazy=True)
    write_cache_entry("fixture", config_fingerprint(a), tools=[{"name": "alpha", "description": "A alpha", "inputSchema": {"type": "object", "properties": {}}}])
    entered, release = _hold_cache_publisher(monkeypatch)
    teardown_done, teardown_go = _gated_reconcile_teardown(monkeypatch)
    target = {"fixture": runtime.descriptor("B", tools={"include": ["beta"]})} if replace else {}
    runtime.publish({"fixture": a})
    with concurrent.futures.ThreadPoolExecutor() as pool:
        registering = pool.submit(mcp.discover_mcp_tools) if entry == "discovery" else pool.submit(mcp.register_mcp_servers, {"fixture": a})
        try:
            assert entered.wait(5)
            mcp._server_connecting_since["fixture"] = time.monotonic() - 999.0
            runtime.publish(target)
            reconciling = pool.submit(mcp.reconcile_mcp_servers, target)
            assert teardown_done.wait(5)
            release.set()
            registering.result(timeout=10)
        finally:
            release.set()
            teardown_go.set()
        outcome = reconciling.result(timeout=10)
    if outcome.get('fixture') in ('refused_connecting','refused_busy'):
        outcome=mcp.reconcile_mcp_servers(target)
    names=set(runtime.registry.get_all_tool_names())
    assert 'mcp__fixture__alpha' not in names, ('stale alpha after successful reconcile',outcome,sorted(names))
    if replace:
        assert outcome=={'fixture':'replaced'}
        assert names==_names(tools=('beta',))
        assert 'B:beta' in mcp._make_tool_handler('fixture','beta',5)({})
    else:
        assert 'fixture' not in mcp._servers and 'fixture' not in mcp._lazy_server_configs


def test_lazy_registration_cannot_publish_after_authoritative_removal(runtime, monkeypatch):
    """Inverted gate probe 1 (.lane/review-gate/test_gate_hunts.py).

    The reviewer's probe asserted the defect: ``reconcile_mcp_servers({})``
    returned ``{}`` (the name had vanished from every snapshotted set) and the
    resumed publisher then resurrected A. Inverted: the name stays reserved
    across the whole publication, the window refusal is truthful
    (``refused_connecting``, asserted as the acceptable outcome), and the
    resumed publisher must NOT reintroduce A.
    """
    from tools.mcp_schema_cache import config_fingerprint, write_cache_entry
    cfg = runtime.descriptor("A", lazy=True)
    write_cache_entry("fixture", config_fingerprint(cfg), tools=[{"name": "beta", "description": "A cached beta", "inputSchema": {"type": "object", "properties": {}}}])
    entered, release = _hold_cache_publisher(monkeypatch)
    teardown_done, teardown_go = _gated_reconcile_teardown(monkeypatch)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        discovery = pool.submit(mcp.register_mcp_servers, {"fixture": cfg})
        try:
            assert entered.wait(5)
            assert 'fixture' in mcp._server_connecting
            assert not mcp._lazy_server_configs and not mcp._servers
            runtime.publish({})
            assert mcp.reconcile_mcp_servers({}) == {"fixture": "refused_connecting"}
            mcp._server_connecting_since["fixture"] = time.monotonic() - 999.0
            reconciling = pool.submit(mcp.reconcile_mcp_servers, {})
            assert teardown_done.wait(5)
            release.set()
            discovery.result(timeout=10)
            assert 'fixture' not in mcp._lazy_server_configs
        finally:
            release.set()
            teardown_go.set()
        result = reconciling.result(timeout=10)
    assert result == {"fixture": "removed"}
    assert 'fixture' not in mcp._servers and 'fixture' not in mcp._lazy_server_configs
    assert 'mcp__fixture__beta' not in runtime.registry.get_all_tool_names()
    answer = mcp._make_tool_handler('fixture', 'beta', 5)({})
    assert 'A:beta' not in answer


def test_lazy_registration_cannot_overwrite_replaced_server_schema(runtime, monkeypatch):
    """Inverted gate probe 2 (.lane/review-gate/test_gate_hunts.py).

    The reviewer's probe asserted the defect: the window reconcile falsely
    returned ``replaced``, then the resumed publisher restored stale alpha
    beside beta and a second reconcile stayed ``unchanged`` leaving the
    excluded tool registered and callable. Inverted: the window refusal is
    truthful, resumed publication must NOT reintroduce alpha, and the retry
    reconcile is authoritative with beta only.

    Note: the reviewer's final defect assertion drove a raw
    ``_make_tool_handler('fixture','alpha',...)`` call, which constructs a
    handler outside the registry and therefore bypasses the registration-time
    include filter by design. The inverted property is registry absence (the
    model can never see the excluded tool), asserted below and re-asserted
    after the second reconcile.
    """
    from tools.mcp_schema_cache import config_fingerprint, write_cache_entry
    cfg = runtime.descriptor("A", lazy=True)
    write_cache_entry("fixture", config_fingerprint(cfg), tools=[{"name": "alpha", "description": "A stale alpha", "inputSchema": {"type": "object", "properties": {}}}])
    entered, release = _hold_cache_publisher(monkeypatch)
    teardown_done, teardown_go = _gated_reconcile_teardown(monkeypatch)
    b = runtime.descriptor("B", tools={"include": ["beta"]})
    with concurrent.futures.ThreadPoolExecutor() as pool:
        discovery = pool.submit(mcp.register_mcp_servers, {"fixture": cfg})
        try:
            assert entered.wait(5)
            assert 'fixture' in mcp._server_connecting
            runtime.publish({"fixture": b})
            assert mcp.reconcile_mcp_servers({"fixture": b}) == {"fixture": "refused_connecting"}
            mcp._server_connecting_since["fixture"] = time.monotonic() - 999.0
            reconciling = pool.submit(mcp.reconcile_mcp_servers, {"fixture": b})
            assert teardown_done.wait(5)
            release.set()
            discovery.result(timeout=10)
            assert 'mcp__fixture__alpha' not in runtime.registry.get_all_tool_names()
        finally:
            release.set()
            teardown_go.set()
        result = reconciling.result(timeout=10)
    assert result == {"fixture": "replaced"}
    names = set(runtime.registry.get_all_tool_names())
    assert 'mcp__fixture__alpha' not in names and 'mcp__fixture__beta' in names
    assert set(mcp._servers['fixture']._registered_tool_names) == _names(tools=('beta',))
    assert mcp._servers['fixture']._config == b
    assert mcp.reconcile_mcp_servers({"fixture": b}) == {"fixture": "unchanged"}
    assert 'mcp__fixture__alpha' not in runtime.registry.get_all_tool_names()


def test_eager_connector_reservation_excludes_reconcile(runtime, monkeypatch):
    """Eager mirror, connect phase: the tracked connector task makes a
    concurrent reconcile answer ``refused_connecting`` (brief v3 prediction),
    and the completion publishes exactly once with a truthful follow-up."""
    cfg = runtime.descriptor("A", connect_timeout=20)
    runtime.publish({"fixture": cfg})
    parked = threading.Event()
    go = threading.Event()
    real_connect = mcp._connect_server

    async def gated_connect(name, config):
        server = await real_connect(name, config)
        parked.set()
        while not go.is_set():
            await asyncio.sleep(0.005)
        return server

    monkeypatch.setattr(mcp, "_connect_server", gated_connect)
    changed = {"fixture": runtime.descriptor("B", tools={"include": ["beta"]})}
    with concurrent.futures.ThreadPoolExecutor() as pool:
        registering = pool.submit(mcp.discover_mcp_tools)
        try:
            assert parked.wait(5)
            assert 'fixture' in mcp._server_connecting
            assert 'fixture' in mcp._server_connect_tasks
            assert mcp.reconcile_mcp_servers(changed) == {"fixture": "refused_connecting"}
            assert 'fixture' not in mcp._servers
        finally:
            go.set()
        registering.result(timeout=15)
    assert 'fixture' not in mcp._server_connecting
    assert set(mcp._servers['fixture']._registered_tool_names) == _names()
    assert mcp.reconcile_mcp_servers(changed) == {"fixture": "replaced"}
    assert 'mcp__fixture__alpha' not in runtime.registry.get_all_tool_names()
    assert mcp._servers['fixture']._config == changed['fixture']


def test_eager_publish_instant_stays_reserved_against_reconcile(runtime):
    """Eager mirror at the publish instant: ``_servers`` is already published
    and tools are still registering, but the reservation plus the tracked
    live connector task still force ``refused_connecting`` — even with a
    marker aged beyond every drain window (the live task, not the age, is the
    fence). This is the state `_connect_and_register_server` occupies between
    its ``_servers[name] = server`` publication and the wrapper's finally."""
    a = runtime.descriptor("A")
    mcp._ensure_mcp_loop()

    async def hold():
        await asyncio.Event().wait()

    async def create():
        return asyncio.create_task(hold())

    connector = mcp._run_on_mcp_loop(create)
    server = mcp.MCPServerTask("fixture")
    server._config = a
    with mcp._lock:
        mcp._server_connecting.add("fixture")
        mcp._server_connecting_since["fixture"] = time.monotonic() - 999.0
        mcp._server_connect_tasks["fixture"] = connector
        mcp._servers["fixture"] = server
    try:
        assert mcp.reconcile_mcp_servers({"fixture": runtime.descriptor("B", tools={"include": ["beta"]})}) == {"fixture": "refused_connecting"}
        assert mcp._servers["fixture"] is server
        assert 'fixture' in mcp._server_connecting
    finally:
        mcp._mcp_loop.call_soon_threadsafe(connector.cancel)


def test_eager_publication_is_one_transition_with_reservation(runtime, monkeypatch):
    """Eager mirror, real publish instant: a parked native tool registration
    proves (a) the reservation is still held at the publish instant, and (b)
    publication is one transition on the MCP loop — a concurrent reconcile
    cannot interleave with it (it stays queued behind the parked publisher),
    so no false acknowledgement can precede or accept a stale eager
    publication. The post-publication reconcile outcome is truthful."""
    cfg = runtime.descriptor("A", connect_timeout=20)
    runtime.publish({"fixture": cfg})
    parked = threading.Event()
    go = threading.Event()
    real_register = mcp._register_server_tools

    def gated_register(name, server, config):
        parked.set()
        assert go.wait(10)
        return real_register(name, server, config)

    monkeypatch.setattr(mcp, "_register_server_tools", gated_register)
    changed = {"fixture": runtime.descriptor("B", tools={"include": ["beta"]})}
    with concurrent.futures.ThreadPoolExecutor() as pool:
        registering = pool.submit(mcp.discover_mcp_tools)
        try:
            assert parked.wait(5)
            assert 'fixture' in mcp._server_connecting
            assert 'fixture' in mcp._server_connect_tasks
            reconciling = pool.submit(mcp.reconcile_mcp_servers, changed)
            time.sleep(0.3)
            assert not reconciling.done()
        finally:
            go.set()
        registering.result(timeout=15)
        assert reconciling.result(timeout=15) == {"fixture": "replaced"}
    assert 'fixture' not in mcp._server_connecting
    names = set(runtime.registry.get_all_tool_names())
    assert 'mcp__fixture__alpha' not in names
    assert names == _names(tools=('beta',))
    assert mcp._servers['fixture']._config == changed['fixture']


# ---------------------------------------------------------------------------
# WEV-1795 v4 — gate-2 expiry hunts, INVERTED (lead ruling LEAD-RULING-2.md).
#
# Driver-for-driver ports of .lane/review-gate-2/test_expiry_hunts.py: the 8
# lazy cases + the default-policy control + the two eager "selected but
# unscheduled" cases (the ruling's named 11) + the file's eager-inside-fence
# disproof (12 items total). The gate-2 file asserted the DEFECT (the old
# publisher resumed and republished A after an authoritative ACK). Every case
# here INVERTS to the ruling's acceptance: old publisher resumes ->
# publication REFUSED at the single generation-compared publication point ->
# registry and lazy maps unchanged -> real RPC on the old name fails or hits
# the NEW server, never ``A:alpha``. Mutant E (drop the generation compare at
# the publication point) turns all of these RED.
# ---------------------------------------------------------------------------

def _record_cache_return(monkeypatch):
    """Record every cache-publisher return; a refused publication returns [].

    Wrap-around for seams that pause INSIDE ``_register_from_cache_sync``.
    """
    published = []
    real = mcp._register_from_cache_sync

    def record(name, config, *args):
        result = real(name, config, *args)
        published.append(list(result))
        return result

    monkeypatch.setattr(mcp, "_register_from_cache_sync", record)
    return published


def _spy_connect(monkeypatch, target_config):
    """Record the transports spawned for *target_config* (teardown proof)."""
    spawned = []
    real_connect = mcp._connect_server

    async def spy(name, config):
        server = await real_connect(name, config)
        if config == target_config:
            spawned.append(server)
        return server

    monkeypatch.setattr(mcp, "_connect_server", spy)
    return spawned


def _record_registry_writes(monkeypatch):
    """Record every (server-level) registry publication attempt's config."""
    registered = []
    real_tools = mcp._register_server_tools

    def record(name, server, config):
        registered.append(config)
        return real_tools(name, server, config)

    monkeypatch.setattr(mcp, "_register_server_tools", record)
    return registered


@pytest.mark.parametrize("entry", ["register", "discovery"])
@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.parametrize("seam", ["entry", "after-check"])
def test_v4_expired_lazy_reservation_publication_refused(runtime, monkeypatch, entry, replace, seam):
    """Inverted gate-2 ``test_expired_lazy_reservation_allows_stale_publication``.

    Same driver: pause the cache publisher at ``_register_from_cache_sync``
    entry (seam='entry') or at ``_record_tool_trust_metadata`` (seam='after-
    check', downstream of the v3 fence), let a REAL reconcile fully complete a
    zero-drain removed/replaced transition and release its fence, then resume
    the old publisher. Inverted: the resumed publication is REFUSED (the
    return is ``[]``) and nothing stale is published.
    """
    from tools.mcp_schema_cache import config_fingerprint, write_cache_entry
    a = runtime.descriptor("A", lazy=True)
    write_cache_entry("fixture", config_fingerprint(a), tools=[{"name": "alpha", "description": "A alpha", "inputSchema": {"type": "object", "properties": {}}}])
    target = {"fixture": runtime.descriptor("B", tools={"include": ["beta"]})} if replace else {}
    entered = threading.Event()
    release = threading.Event()
    published = []
    if seam == "entry":
        attr = "_register_from_cache_sync"
    else:
        published = _record_cache_return(monkeypatch)
        attr = "_record_tool_trust_metadata"
    real_attr = getattr(mcp, attr)

    def held(name, config, *args):
        if config == a:
            entered.set()
            assert release.wait(25)
        result = real_attr(name, config, *args)
        if attr == "_register_from_cache_sync":
            published.append(list(result))
        return result

    monkeypatch.setattr(mcp, attr, held)
    runtime.publish({"fixture": a})
    with concurrent.futures.ThreadPoolExecutor() as pool:
        publishing = pool.submit(mcp.discover_mcp_tools) if entry == "discovery" else pool.submit(mcp.register_mcp_servers, {"fixture": a})
        try:
            assert entered.wait(5)
            assert "fixture" in mcp._server_connecting
            assert "fixture" not in mcp._server_connect_tasks
            runtime.publish(target)
            outcome = mcp.reconcile_mcp_servers(target, drain_seconds=0)
            assert outcome == {"fixture": "replaced" if replace else "removed"}
            assert not mcp._mcp_reconciling
            assert "mcp__fixture__alpha" not in runtime.registry.get_all_tool_names()
        finally:
            release.set()
        publishing.result(timeout=10)
    # INVERTED: gate-2 asserted stale alpha / A:alpha here.
    assert published and published[-1] == [], ("publication was not refused", published)
    names = set(runtime.registry.get_all_tool_names())
    assert "mcp__fixture__alpha" not in names, ("stale alpha after authoritative ACK", seam, entry, replace, outcome, sorted(names))
    assert mcp._lazy_server_configs.get("fixture") is None
    if replace:
        assert mcp._servers["fixture"]._config == target["fixture"]
        assert mcp.reconcile_mcp_servers(target) == {"fixture": "unchanged"}
        answer = mcp._make_tool_handler("fixture", "alpha", 5)({})
        assert "B:alpha" in answer and "A:alpha" not in answer
    else:
        assert "fixture" not in mcp._servers and "fixture" not in mcp._lazy_server_configs
        answer = mcp._make_tool_handler("fixture", "alpha", 5)({})
        assert "A:alpha" not in answer
    print("ACCEPT stale lazy publication refused", seam, entry, replace, outcome)


def test_v4_default_drain_expired_live_lazy_publisher_publication_refused(runtime, monkeypatch):
    """Inverted gate-2 ``test_default_drain_naturally_expires_live_lazy_publisher``.

    No timestamp or fence injection: DEFAULT policy, the first reconcile
    truthfully refuses, the stranded-marker expiry (default 10s) lets the
    second reconcile transition, and the resumed publisher is REFUSED.
    """
    from tools.mcp_schema_cache import config_fingerprint, write_cache_entry
    a = runtime.descriptor("A", lazy=True)
    write_cache_entry("fixture", config_fingerprint(a), tools=[{"name": "alpha", "description": "A alpha", "inputSchema": {}}])
    entered = threading.Event()
    release = threading.Event()
    published = []
    real = mcp._register_from_cache_sync

    def held(*args):
        entered.set()
        assert release.wait(25)
        result = real(*args)
        published.append(list(result))
        return result

    monkeypatch.setattr(mcp, "_register_from_cache_sync", held)
    runtime.publish({"fixture": a})
    with concurrent.futures.ThreadPoolExecutor() as pool:
        publishing = pool.submit(mcp.register_mcp_servers, {"fixture": a})
        try:
            assert entered.wait(5)
            runtime.publish({})
            assert mcp.reconcile_mcp_servers({}) == {"fixture": "refused_connecting"}
            time.sleep(10.1)
            assert time.monotonic() - mcp._server_connecting_since["fixture"] > 10
            assert mcp.reconcile_mcp_servers({}) == {"fixture": "removed"}
        finally:
            release.set()
        publishing.result(timeout=10)
    # INVERTED: gate-2 asserted alpha reappearing and A:alpha here.
    assert published and published[-1] == [], ("publication was not refused", published)
    assert "mcp__fixture__alpha" not in runtime.registry.get_all_tool_names()
    assert "fixture" not in mcp._servers and "fixture" not in mcp._lazy_server_configs
    answer = mcp._make_tool_handler("fixture", "alpha", 5)({})
    assert "A:alpha" not in answer
    print("ACCEPT default 10s expiry; live lazy publisher resumed and was refused")


@pytest.mark.parametrize("replace", [False, True])
def test_v4_selected_but_unscheduled_eager_publication_refused(runtime, monkeypatch, replace):
    """Inverted gate-2 ``test_selected_but_unscheduled_eager_connector_outlives_reconcile``.

    The eager selection-to-task gap: an old caller selected before a zero-drain
    reconcile (which expires the taskless marker and fully completes
    removed/replaced) resumes, really connects A, and must then be REFUSED at
    the single publication point — no registry write, its own transport torn
    down, and the authoritative result left in place.
    """
    a = runtime.descriptor("A")
    entered = threading.Event()
    release = threading.Event()
    real = mcp._resolve_server_lazy

    def held(name, cfg):
        if cfg == a:
            entered.set()
            assert release.wait(15)
        return real(name, cfg)

    monkeypatch.setattr(mcp, "_resolve_server_lazy", held)
    target = {"fixture": runtime.descriptor("B", tools={"include": ["beta"]})} if replace else {}
    runtime.publish({"fixture": a})
    spawned = _spy_connect(monkeypatch, a)
    registered = _record_registry_writes(monkeypatch)
    replacement = None
    try:
        with concurrent.futures.ThreadPoolExecutor() as pool:
            publishing = pool.submit(mcp.register_mcp_servers, {"fixture": a})
            try:
                assert entered.wait(5)
                assert "fixture" in mcp._server_connecting
                assert not mcp._server_connect_tasks
                runtime.publish(target)
                assert mcp.reconcile_mcp_servers(target, drain_seconds=0) == {"fixture": "replaced" if replace else "removed"}
                replacement = mcp._servers.get("fixture")
            finally:
                release.set()
            publishing.result(timeout=10)
        # INVERTED: gate-2 asserted _servers['fixture']._config == a and A:alpha.
        assert a not in registered, ("stale eager attempt reached the registry", registered)
        assert spawned and spawned[-1].session is None, "stale attempt's transport was not torn down"
        names = set(runtime.registry.get_all_tool_names())
        assert "mcp__fixture__alpha" not in names, ("stale alpha after authoritative ACK", replace, sorted(names))
        if replace:
            assert mcp._servers.get("fixture") is replacement
            assert mcp._servers["fixture"]._config == target["fixture"]
            assert mcp.reconcile_mcp_servers(target) == {"fixture": "unchanged"}
            answer = mcp._make_tool_handler("fixture", "alpha", 5)({})
            assert "B:alpha" in answer and "A:alpha" not in answer
        else:
            assert "fixture" not in mcp._servers and "fixture" not in mcp._lazy_server_configs
            answer = mcp._make_tool_handler("fixture", "alpha", 5)({})
            assert "A:alpha" not in answer
        print("ACCEPT delayed eager connector refused", replace, sorted(names))
    finally:
        if replacement is not None and mcp._servers.get("fixture") is not replacement:
            mcp._run_on_mcp_loop(replacement.shutdown, timeout=5)
        for server in spawned:
            if getattr(server, "session", None) is not None:
                mcp._run_on_mcp_loop(server.shutdown, timeout=5)


def test_v4_eager_predecessor_publication_refused_inside_active_reconcile(runtime, monkeypatch):
    """Inverted gate-2 ``test_eager_predecessor_can_publish_inside_active_reconcile``.

    The resumed selected-but-unscheduled eager predecessor runs WHILE the
    reconcile transition is still open (post-teardown seam). Inverted: the
    predecessor is REFUSED even inside the fence (the generation increment
    precedes the fence), never registers A, never touches ``_servers``, and
    tears down its own transport; B then completes the transition cleanly.
    """
    a = runtime.descriptor("A")
    b = runtime.descriptor("B", tools={"include": ["beta"]})
    selected = threading.Event()
    go = threading.Event()
    torn = threading.Event()
    finish = threading.Event()
    real_lazy = mcp._resolve_server_lazy
    real_teardown = mcp._teardown_mcp_server
    registered = _record_registry_writes(monkeypatch)
    spawned = _spy_connect(monkeypatch, a)

    def parked(name, cfg):
        if cfg == a:
            selected.set()
            assert go.wait(15)
        return real_lazy(name, cfg)

    async def teardown(name, server):
        await real_teardown(name, server)
        torn.set()
        while not finish.is_set():
            await asyncio.sleep(.005)

    monkeypatch.setattr(mcp, "_resolve_server_lazy", parked)
    monkeypatch.setattr(mcp, "_teardown_mcp_server", teardown)
    runtime.publish({"fixture": a})
    try:
        with concurrent.futures.ThreadPoolExecutor() as pool:
            registering = pool.submit(mcp.register_mcp_servers, {"fixture": a})
            try:
                assert selected.wait(5)
                runtime.publish({"fixture": b})
                reconciling = pool.submit(mcp.reconcile_mcp_servers, {"fixture": b}, drain_seconds=0)
                assert torn.wait(5)
                go.set()
                registering.result(timeout=10)
                # INVERTED: gate-2 asserted stale._config == a here (the stale
                # eager publisher had run INSIDE _mcp_reconciling).
                assert a not in registered, ("stale predecessor reached the registry", registered)
                assert "fixture" not in mcp._servers
                assert "mcp__fixture__alpha" not in runtime.registry.get_all_tool_names()
            finally:
                go.set()
                finish.set()
            assert reconciling.result(timeout=10) == {"fixture": "replaced"}
        assert mcp._servers["fixture"]._config == b
        assert "mcp__fixture__alpha" not in runtime.registry.get_all_tool_names()
        assert set(mcp._servers["fixture"]._registered_tool_names) == _names(tools=("beta",))
        assert mcp.reconcile_mcp_servers({"fixture": b}) == {"fixture": "unchanged"}
        answer = mcp._make_tool_handler("fixture", "alpha", 5)({})
        assert "B:alpha" in answer and "A:alpha" not in answer
        assert spawned and spawned[-1].session is None, "stale predecessor's transport was not torn down"
        print("ACCEPT stale EAGER predecessor refused inside the open transition")
    finally:
        for server in spawned:
            if getattr(server, "session", None) is not None:
                mcp._run_on_mcp_loop(server.shutdown, timeout=5)
