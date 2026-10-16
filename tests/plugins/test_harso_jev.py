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


def test_named_window_on_another_site_goes_to_the_requested_page(runtime, monkeypatch):
    """Abhi 2026-10-17: target + a new url on another site failed with LeftAllowlist instead of opening it."""
    announced = []
    monkeypatch.setattr(runner.announce, "publish", lambda target, url: announced.append(target), raising=False)
    runtime.url = "https://other.test/"
    result = runner.run({"url": "https://example.com/", "target": "owned"}, creds())
    assert result["success"] and result["browser_page"]["target"] == "owned"
    assert ("Page.navigate", {"url": "https://example.com/"}) in runtime.calls
    assert not any(method == "Target.createTarget" for method, _ in runtime.calls)
    assert announced == []  # the window already has its card


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
    assert set(plugin.SCHEMA["parameters"]["properties"]) == {"url", "goal", "expect", "allow_hosts", "target", "text_offset", "new_window"}


@pytest.mark.parametrize("activity_failure", [False, True])
def test_handler_routes_existing_engine_and_keeps_credentials_off_argv(monkeypatch, activity_failure):
    from agent import redact, secret_scope
    from gateway.native_bot_admission import native_turn_context
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
    token = native_turn_context.set({
        "session_id": "native-session", "run_id": "01990000-0000-7000-8000-000000000001"})
    try:
        result = json.loads(plugin.browse({"url": "https://example.com/"}, task_id="task"))
    finally:
        native_turn_context.reset(token)
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
    assert observed["payload"]["credentials"]["WEAVE_HERMES_SESSION_ID"] == "native-session"  # never the static secret
    assert observed["timeout"] == 300


def test_browse_outside_a_native_turn_is_refused(monkeypatch):
    from tools import browser_use_cli as engine
    monkeypatch.setattr(engine, "_code_url_verdict", lambda _: SimpleNamespace(blocked=None, local_sidecar=False))
    monkeypatch.setattr(engine, "_base_subprocess_env", lambda: {"BU_CDP_WS": "ws://cdp.test/"})
    monkeypatch.setattr(engine, "_route_backend", lambda *a, **kw: None)
    monkeypatch.setattr(engine, "_run_cli_killing_process_group", lambda *a, **kw: pytest.fail("worker started"))
    assert not json.loads(plugin.browse({"url": "https://example.com/"}, task_id="task"))["success"]


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


def test_card_is_announced_when_the_window_opens_not_when_jev_ends(runtime, monkeypatch):
    """Abhi 2026-10-16: the browser card shows the moment the window opens, so it streams in real time."""
    owned = "B" * 32
    from browser_harness import helpers
    from jev_ultrafast import browser
    real = helpers.cdp
    def cdp(method, session_id=None, **params):
        if method == "Target.createTarget":
            runtime.calls.append((method, params))
            return {"targetId": owned}
        return real(method, session_id, **params)
    monkeypatch.setattr(helpers, "cdp", cdp)
    monkeypatch.setattr(browser, "cdp", cdp)
    seen = []
    monkeypatch.setattr(runner, "ANNOUNCED", [])
    monkeypatch.setattr(runner, "_ANNOUNCERS", [])
    def publish(target, url):
        seen.append((target, url, [m for m, _ in runtime.calls].count("Runtime.evaluate")))
    monkeypatch.setattr(runner.announce, "publish", publish, raising=False)
    result = runner.run({"url": runtime.url}, creds())
    for thread in runner._ANNOUNCERS:
        thread.join(1)
    assert result["browser_page"]["target"] == owned
    assert [(t, u) for t, u, _ in seen] == [(owned, runtime.url)]
    assert runner.ANNOUNCED == [owned]
    runtime.calls.clear()
    seen.clear()
    runner.run({"url": runtime.url}, creds())  # the same window again: one card, not two
    assert seen == []


def test_activity_publisher_needs_bearer_and_session():
    assert runner.activity_publisher({"WEAVE_API_URL": "https://api.test"}) is None
    assert callable(runner.activity_publisher({"WEAVE_API_URL": "https://api.test", "WEAVE_API_MCP_BEARER": "wvc1_x",
                                               "WEAVE_HERMES_SESSION_ID": "s"}))


REUSED, STALE = "C" * 32, "D" * 32


def test_a_chat_keeps_working_in_its_window_and_idle_jev_windows_close(runtime):
    """Abhi 2026-10-16: follow-up steps reuse the chat's window (same card); idle Jev windows close."""
    runtime.targets += [{"type": "page", "targetId": REUSED, "url": "https://example.com/"},
                        {"type": "page", "targetId": STALE, "url": "https://old.example/"}]
    result = runner.run({"url": runtime.url, "reuse": REUSED, "close": [STALE, "E" * 32]}, creds())
    assert result["browser_page"]["target"] == REUSED
    assert not any(method == "Target.createTarget" for method, _ in runtime.calls)
    assert ("Page.navigate", {"url": runtime.url}) in runtime.calls
    assert [p["targetId"] for m, p in runtime.calls if m == "Target.closeTarget"] == [STALE]  # gone one: no call


def test_a_vanished_chat_window_opens_a_new_one(runtime):
    result = runner.run({"url": runtime.url, "reuse": REUSED}, creds())
    assert result["browser_page"]["target"] == "owned"
    assert ("Target.createTarget", {"url": "about:blank", "newWindow": True}) in runtime.calls


def test_window_memory_reuses_per_chat_and_expires_idle_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "_windows_file", lambda: tmp_path / "windows.json")
    plugin._remember_window("chat-a", REUSED, 1000)
    plugin._remember_window("chat-b", STALE, 1000)
    assert plugin._plan_windows("chat-a", 1000 + 60, 3600) == (REUSED, [])
    assert plugin._plan_windows("chat-c", 1000 + 60, 3600) == ("", [])
    plugin._remember_window("chat-a", REUSED, 5000)  # used again: its idle clock restarts
    assert plugin._plan_windows("chat-a", 1000 + 3601, 3600) == (REUSED, [STALE])
    assert json.loads((tmp_path / "windows.json").read_text()) == {"chats": {"chat-a": REUSED},
                                                                    "windows": {REUSED: 5000}}


def test_handler_hands_the_chat_window_to_the_worker_unless_new_window(tmp_path, monkeypatch):
    from agent import redact, secret_scope
    from gateway.native_bot_admission import native_turn_context
    from tools import browser_use_cli as engine

    monkeypatch.setattr(plugin, "_windows_file", lambda: tmp_path / "windows.json")
    monkeypatch.setattr(redact, "redact_sensitive_text", lambda value, **kw: value)
    monkeypatch.setattr(secret_scope, "get_secret", lambda key, *a: {"WEAVE_API_URL": ""}.get(key))
    monkeypatch.setattr(engine, "_code_url_verdict", lambda _: SimpleNamespace(blocked=None, local_sidecar=False))
    monkeypatch.setattr(engine, "_base_subprocess_env", lambda: {"BU_CDP_WS": "ws://cdp.test/"})
    monkeypatch.setattr(engine, "_route_backend", lambda *a, **kw: None)
    monkeypatch.setattr(engine, "_attach_vault_supervisor", lambda *a: None)
    monkeypatch.setattr(engine, "_driven_daemons", set())
    sent = []

    def worker(cmd, payload, env, timeout):
        sent.append(json.loads(payload)["args"])
        return SimpleNamespace(returncode=0, stdout=json.dumps({"success": True, "browser_page": {
            "target": REUSED, "url": "https://example.com/", "title": ""}, "_announced": [REUSED]}))

    monkeypatch.setattr(engine, "_run_cli_killing_process_group", worker)
    token = native_turn_context.set({"session_id": "chat-a", "run_id": "01990000-0000-7000-8000-000000000001"})
    try:
        for args in ({"url": "https://example.com/"}, {"url": "https://example.com/next"},
                     {"url": "https://other.test/", "new_window": True}):
            assert json.loads(plugin.browse(args, task_id="task"))["success"]
    finally:
        native_turn_context.reset(token)
    assert sent == [{"url": "https://example.com/"}, {"url": "https://example.com/next", "reuse": REUSED},
                    {"url": "https://other.test/"}]


def test_a_window_that_never_loaded_is_closed_not_left_blank(runtime):
    runtime.targets = [{"type": "page", "targetId": "user-tab", "url": "https://user.example/"}]
    runtime.navigate_to = "about:blank"
    with pytest.raises(runner.LeftAllowlist):
        runner.run({"url": "https://example.com/"}, creds(), navigation_timeout=0.01)
    assert [p["targetId"] for m, p in runtime.calls if m == "Target.closeTarget"] == ["owned"]


def test_a_failed_step_in_an_existing_window_never_closes_it(runtime):
    runtime.navigate_to = "about:blank"
    with pytest.raises(runner.LeftAllowlist):
        runner.run({"url": "https://example.com/", "reuse": "A" * 32}, creds(), navigation_timeout=0.01)
    runtime.calls.clear()
    with pytest.raises(runner.LeftAllowlist):
        runner.run({"url": "https://example.com/", "target": "owned"}, creds(), navigation_timeout=0.01)
    assert not any(method == "Target.closeTarget" for method, _ in runtime.calls)


@pytest.mark.parametrize("host,family", [
    ("redmart.lazada.sg", "lazada.sg"), ("www.amazon.com", "amazon.com"), ("a.b.example.com", "b.example.com"),
    ("lazada.sg", ""), ("www.bbc.co.uk", "bbc.co.uk"), ("bbc.co.uk", ""), ("shop.com.sg", ""),
    ("www.gov.sg", ""), ("www.shopee.com.sg", "shopee.com.sg"),
    ("the-internet.herokuapp.com", ""), ("me.github.io", ""), ("app.vercel.app", "")])
def test_site_family_widens_to_the_owner_never_a_shared_parent(host, family):
    assert runner.site_family(host) == family


def test_sibling_hosts_of_the_start_site_are_allowed(runtime):
    """RedMart sends the agent to www.lazada.sg; that failed with LeftAllowlist on every first try."""
    runtime.navigate_to = "https://www.lazada.sg/catalog/?q=porcini"
    result = runner.run({"url": "https://redmart.lazada.sg/"}, creds())
    assert result["success"] and result["final_url"] == "https://www.lazada.sg/catalog/?q=porcini"


def test_a_dns_blip_is_retried_before_the_url_is_blocked(monkeypatch):
    import socket
    from tools import url_safety
    answers = [socket.gaierror(-3, "Temporary failure in name resolution"),
               [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("47.246.167.3", 0))]]

    def flaky(hostname, port=None):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(url_safety, "_getaddrinfo", flaky)
    monkeypatch.setattr(url_safety.time, "sleep", lambda _: None)
    assert url_safety.is_safe_url("https://redmart.lazada.sg/")


def test_a_failed_lookup_says_so_instead_of_private_address(monkeypatch):
    import socket
    from tools import browser_use_cli as engine
    monkeypatch.setattr(engine, "_code_url_verdict",
                        lambda _: SimpleNamespace(blocked="Blocked: URL targets a private or internal address", local_sidecar=False))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(socket.gaierror(-3, "x")))
    result = json.loads(plugin.browse({"url": "https://redmart.lazada.sg/"}, task_id="task"))
    assert result["error"] == "Could not look up redmart.lazada.sg (DNS failed). Try again in a moment."
