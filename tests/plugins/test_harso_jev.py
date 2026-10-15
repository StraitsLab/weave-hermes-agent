"""Real pinned Jev loop with fake CDP and model; no Chrome, network or credentials."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "harso-jev"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = load("hjev_runner", PLUGIN / "runner.py")
plugin = load("hjev_plugin", PLUGIN / "__init__.py")


@pytest.fixture
def runtime(monkeypatch):
    from browser_harness import admin, helpers
    from jev_ultrafast import agent, browser, model

    state = SimpleNamespace(url="https://example.com/", text="Hello world", title="Example", calls=[],
                            choice="DONE", decisions=0, navigate_to=None, ready="complete", targets=[
                                {"type": "page", "targetId": "user-tab", "url": "https://user.example/"},
                                {"type": "page", "targetId": "owned", "url": "https://example.com/"},
                            ])

    def cdp(method, session_id=None, **params):
        state.calls.append((method, params))
        if method == "Target.getTargets":
            return {"targetInfos": state.targets}
        if method == "Target.createTarget":
            return {"targetId": "owned"}
        if method == "Target.attachToTarget":
            return {"sessionId": "jev-session"}
        if method == "Page.navigate":
            state.url = state.navigate_to or params["url"]
            return {}
        if method == "Runtime.evaluate":
            expression = params["expression"]
            if expression == browser.READ_STATE:
                value = {"url": state.url, "title": state.title, "text": state.text,
                         "actions": [{"id": "wait", "kind": "wait", "label": "Wait"}],
                         "scroll": {}, "marker": "current", "guards": {}, "page_key": "key"}
            elif expression == browser.MARKER:
                value = "current"
            else:
                value = {"location.href": state.url, "document.readyState": state.ready,
                         "document.title": state.title, runner.PAGE_TEXT_JS: state.text,
                         "(document.querySelector('h1,h2')||{}).textContent||''": "Heading"}.get(expression, False)
            return {"result": {"value": value}}
        return {}

    def choose(*args):
        state.decisions += 1
        return {"choice": state.choice, "probabilities": {state.choice: 1}, "confidence": 1,
                "latency_ms": 0, "operation": "WAIT", "target": None, "usage": {}}

    monkeypatch.setattr(helpers, "cdp", cdp)
    monkeypatch.setattr(helpers, "switch_tab", lambda target, **kw: state.calls.append(("switch_tab", {"target": target})))
    monkeypatch.setattr(admin, "ensure_daemon", lambda: None)
    monkeypatch.setattr(browser, "cdp", cdp)
    monkeypatch.setattr(agent, "choose", choose)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    # configure_runtime modifies process-global Jev modules, isolated in production worker processes.
    monkeypatch.setattr(agent, "Browser", agent.Browser)
    monkeypatch.setattr(model, "post_json", model.post_json)
    monkeypatch.setattr(model, "os", model.os)
    monkeypatch.setattr(model.CLIENT, "event_hooks", {"request": []})
    return state


def creds():
    return runner.resolve_credentials({"WEAVE_API_URL": "https://api.test", "WEAVE_API_MCP_BEARER": "attempt-test"})


def test_owned_tab_and_resume_without_navigation(runtime):
    result = runner.run({"url": runtime.url}, creds())
    assert result["success"] and result["browser_page"]["target"] == "owned"
    assert ("Target.createTarget", {"url": "about:blank", "newWindow": True}) in runtime.calls
    assert not any(method == "Target.closeTarget" for method, _ in runtime.calls)
    runtime.calls.clear()
    runner.run({"url": runtime.url, "target": "owned"}, creds())
    assert not any(method in {"Page.navigate", "Target.createTarget"} for method, _ in runtime.calls)
    assert any(method == "Target.detachFromTarget" for method, _ in runtime.calls)


def test_unavailable_target_refused(runtime):
    with pytest.raises(ValueError, match="unavailable"):
        runner.run({"url": runtime.url, "target": "missing"}, creds())
    assert not any(method == "Page.navigate" for method, _ in runtime.calls)


def test_allowlist_refuses_redirect_before_model_or_page_text(runtime):
    runtime.navigate_to = "https://example.com.evil.test/"
    with pytest.raises(runner.LeftAllowlist):
        runner.run({"url": runtime.url, "goal": "Go"}, creds())
    assert runtime.decisions == 0
    from jev_ultrafast.browser import READ_STATE
    assert not any(method == "Runtime.evaluate" and params["expression"] == READ_STATE for method, params in runtime.calls)


def test_allowlist_refuses_resume(runtime):
    runtime.url = "https://other.test/"
    with pytest.raises(runner.LeftAllowlist):
        runner.run({"url": "https://example.com/", "target": "owned"}, creds())
    assert runtime.decisions == 0


def test_tick_budget_stops_real_jev_loop(runtime):
    runtime.choice = "wait"
    result = runner.run({"url": runtime.url, "goal": "Keep waiting"}, creds(), max_ticks=2)
    assert result["ticks"] == runtime.decisions == 2
    assert result["status"] == "blocked" and not result["success"]
    assert "2-step budget" in result["blocked_reason"]


@pytest.mark.parametrize("expect,verified", [("world", True), ("missing", False), ("", False)])
def test_done_is_not_independent_verification(runtime, expect, verified):
    result = runner.run({"url": runtime.url, "goal": "Finish", "expect": expect}, creds())
    assert result["status"] == "done"
    assert result["verified"] is verified
    assert result["success"] is (verified or not expect)


def test_excerpt_offset_and_expect_checks_full_live_text(runtime):
    runtime.text = "a" * 6100 + "needle" + "z" * 7000
    first = runner.run({"url": runtime.url, "expect": "needle"}, creds())
    assert first["verified"] and "needle" not in first["text"]
    assert "text_offset=6000" in first["text"]
    second = runner.run({"url": runtime.url, "target": "owned", "text_offset": 6000}, creds())
    assert second["text"].startswith("a" * 100 + "needle")
    assert runner.page_excerpt(" a  b \n c ", offset=2) == "b\nc"


def test_navigation_timeout_detaches(runtime, monkeypatch):
    runtime.ready = "loading"
    clock = iter([0, 1])
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(clock))
    with pytest.raises(runner.NavigationTimeout):
        runner.run({"url": runtime.url}, creds(), navigation_timeout=0.1)
    assert any(method == "Target.detachFromTarget" for method, _ in runtime.calls)
    assert runtime.decisions == 0


def test_proxy_urls_bearer_and_session_header(runtime, monkeypatch):
    from jev_ultrafast import model
    calls = []
    monkeypatch.setattr(model, "post_json", lambda *args: calls.append(args))
    resolved = runner.resolve_credentials({"WEAVE_API_URL": "https://api.test/", "WEAVE_API_MCP_BEARER": "wvc1_test",
                                           "WEAVE_HERMES_SESSION_ID": "session-test"})
    runner.configure_runtime(SimpleNamespace(target=""), resolved, ["example.com"])
    model.post_json("https://api.typesafe.ai/v1/systemone", "ignored", {})
    model.post_json("https://provider.test/chat/completions", "ignored", {})
    assert [call[:2] for call in calls] == [("https://api.test/v1/jev/systemone", "wvc1_test"),
                                           ("https://api.test/v1/jev/text/chat/completions", "wvc1_test")]
    hook = model.CLIENT.event_hooks["request"][-1]
    for url, session in [(calls[0][0], None), (calls[1][0], "session-test")]:
        request = httpx.Request("POST", url, headers={"Weave-Hermes-Session": "stale"})
        hook(request)
        assert request.headers.get("Weave-Hermes-Session") == session
    with pytest.raises(ValueError, match="session context"):
        runner.resolve_credentials({"WEAVE_API_URL": "https://api.test", "WEAVE_API_MCP_BEARER": "wvc1_test"})


def test_browse_is_vault_guarded(monkeypatch):
    from tools import harso_vault_guard as guard
    monkeypatch.setattr(guard, "_shared", lambda _: True)
    monkeypatch.setattr(guard, "armed", lambda _: True)
    result = json.loads(guard.protect(tool_name="browse", args={}, next_call=lambda _: pytest.fail("escaped")))
    assert result["error_type"] == "vault_armed"


def test_registers_exactly_one_tool():
    registered = []
    plugin.register(SimpleNamespace(register_tool=lambda **kw: registered.append(kw)))
    assert [r["name"] for r in registered] == ["browse"]
    assert set(plugin.SCHEMA["parameters"]["properties"]) == {"url", "goal", "expect", "allow_hosts", "target", "text_offset"}


@pytest.mark.parametrize("activity_failure", [False, True])
def test_handler_routes_existing_engine_and_keeps_credentials_off_argv(monkeypatch, activity_failure):
    from agent import redact, secret_scope
    from gateway.session_api_turn import api_execution
    from tools import browser_use_cli as engine, browser_tool_session

    observed = {}
    monkeypatch.setattr(redact, "redact_sensitive_text", lambda value, **kw: value)
    bearer = "wvc1_" + "V" * 43
    monkeypatch.setattr(secret_scope, "get_secret", lambda key, *a: {"WEAVE_API_URL": "https://api.test/",
                          "WEAVE_API_MCP_BEARER": bearer, "WEAVE_HERMES_SESSION_ID": "stale-session"}.get(key))

    def post(request):
        observed["activity"] = request
        if activity_failure:
            raise httpx.ReadTimeout("unreachable")
        return httpx.Response(200, json={"ok": True})

    client_class = httpx.Client
    def client(**kwargs):
        observed["http_options"] = kwargs
        return client_class(transport=httpx.MockTransport(post), **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setattr(engine, "_code_url_verdict", lambda _: SimpleNamespace(blocked=None, local_sidecar=False))
    monkeypatch.setattr(engine, "_base_subprocess_env", lambda: {"BU_CDP_WS": "ws://cdp.test/", "WEAVE_API_MCP_BEARER": "remove"})

    def route(env, session, task_id, local, **kw):
        observed["route"] = (session, task_id, local)
        env[engine._BOT_DESKTOP_BROWSER_SENTINEL] = "1"

    monkeypatch.setattr(engine, "_route_backend", route)
    monkeypatch.setattr(engine, "_attach_vault_supervisor", lambda env, task: observed.update(supervisor=task))
    monkeypatch.setattr(engine, "_driven_daemons", set())
    monkeypatch.setattr(browser_tool_session, "run_fenced", lambda info, fn: observed.update(fence=info) or fn())

    def worker(cmd, payload, env, timeout):
        observed.update(cmd=cmd, payload=json.loads(payload), env=env, timeout=timeout)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"success": True, "browser_page": {
            "target": "A" * 32, "url": "https://example.com/", "title": "Example"}}))

    monkeypatch.setattr(engine, "_run_cli_killing_process_group", worker)
    token = api_execution.set({"relay_metadata": {"harso_native_v1": {
        "session_id": "native-session", "run_id": "01990000-0000-7000-8000-000000000001"}}})
    try:
        result = json.loads(plugin.browse({"url": "https://example.com/"}, task_id="task"))
    finally:
        api_execution.reset(token)
    assert result == {"success": True, "browser_page": {
        "target": "A" * 32, "url": "https://example.com/", "title": "Example"}}
    assert str(observed["activity"].url) == "https://api.test/v1/browser/activity"
    assert observed["activity"].headers["authorization"] == f"Bearer {bearer}"
    assert json.loads(observed["activity"].content) == {"session_id": "native-session", **result["browser_page"]}
    assert observed["http_options"] == {"timeout": 3.0, "follow_redirects": False}
    assert observed["route"] == ("", "task", False)
    assert observed["supervisor"] == "task"
    assert observed["fence"] == {"features": {"local": True}}
    assert observed["env"]["BU_CDP_WS"] == "ws://cdp.test/"
    assert "WEAVE_API_MCP_BEARER" not in observed["env"]
    assert bearer not in " ".join(observed["cmd"])
    assert observed["payload"]["credentials"]["WEAVE_API_MCP_BEARER"] == bearer
    assert observed["timeout"] == 300


@pytest.mark.parametrize("args", [{"url": "http://example.com"}, {"url": "https://user:pass@example.com"},
    {"url": "https://example.com", "text_offset": -1}, {"url": "https://example.com", "text_offset": True},
    {"url": "https://example.com", "target": "foreign"}, {"url": "https://example.com", "use_vault": True}])
def test_handler_rejects_bad_input_before_backend(args, monkeypatch):
    from tools import browser_use_cli as engine
    monkeypatch.setattr(engine, "_route_backend", lambda *a, **kw: pytest.fail("backend reached"))
    assert not json.loads(plugin.browse(args))["success"]


def test_login_blocks_model_and_keeps_page_for_native_vault(runtime, monkeypatch):
    from browser_harness import helpers
    from jev_ultrafast import browser
    original = helpers.cdp

    def login_cdp(method, **params):
        if method == "Runtime.evaluate" and "input[type=password]" in params.get("expression", ""):
            return {"result": {"value": True}}
        return original(method, **params)

    monkeypatch.setattr(helpers, "cdp", login_cdp)
    monkeypatch.setattr(browser, "cdp", login_cdp)
    result = runner.run({"url": runtime.url, "goal": "Sign in"}, creds())
    assert result["error_type"] == "NativeLoginRequired"
    assert result["browser_page"] == {"target": "owned", "url": runtime.url, "title": runtime.title}
    assert not runtime.decisions
    assert any(method == "Target.detachFromTarget" for method, _ in runtime.calls)


def test_real_plugin_discovery_exposes_browse(tmp_path, monkeypatch):
    from hermes_cli import plugins
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("plugins:\n  enabled: [harso-jev]\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = plugins.PluginManager()
    manager.discover_and_load()
    assert "browse" in manager._plugin_tool_names
