"""Bot Screen wiring in the fork's browser tool (tools/bot_desktop/UPSTREAM.md, "Browser-tool wiring").

Each test drives the fork's real ``tools.browser_tool`` path down to ``subprocess.Popen`` (stubbed): the
agent-browser argv/env are what the tool would really spawn.

T-1a-2: a human lease refuses the command before anything is spawned.
T-1a-3: a takeover while the command ran voids its result.
T-1a-5: the janitor keeps a shared browser a human holds, past the inactivity timeout (fake clock).
T-1a-6: the daemon idle timer is 24 h while a screen is published, the janitor's timeout otherwise.
"""

from __future__ import annotations

import json
import os

import pytest

import tools.browser_tool as bt
from tools.bot_desktop import lease, runtime

_SECRET = "WHAT-THE-HUMAN-TYPED"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    lease._reset_for_tests()
    bt._active_sessions.clear()
    bt._session_last_activity.clear()
    bt._last_active_session_key.clear()
    for key in ("AGENT_BROWSER_IDLE_TIMEOUT_MS", "AGENT_BROWSER_PROFILE", "AGENT_BROWSER_EXECUTABLE_PATH",
                "HERMES_BD_STATE_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(runtime, "published_env", lambda: {"DISPLAY": ":37"})
    monkeypatch.setattr(runtime, "ensure_started_for_tool", lambda: None)
    monkeypatch.setattr(runtime, "touch_activity", lambda: None)
    yield
    lease._reset_for_tests()
    bt._active_sessions.clear()
    bt._session_last_activity.clear()
    bt._last_active_session_key.clear()


class _Proc:
    returncode = 0

    def __init__(self, on_wait=None):
        self._on_wait = on_wait

    def wait(self, timeout=None):
        if self._on_wait:
            self._on_wait()
        return 0

    def kill(self):
        pass


def _wire(monkeypatch, *, session=None, on_wait=None, headed=True):
    """Local agent-browser session; Popen writes the page's reply into the command's stdout file."""
    spawned: list = []
    info = session or {"session_name": "h_bot", "bb_session_id": None, "cdp_url": None, "features": {"local": True}}
    monkeypatch.setattr(bt, "_find_agent_browser", lambda: "agent-browser")
    monkeypatch.setattr(bt, "_requires_real_termux_browser_install", lambda cmd: False)
    monkeypatch.setattr(bt, "_is_local_mode", lambda: True)
    monkeypatch.setattr(bt, "_chromium_installed", lambda: True)
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(bt, "_get_browser_engine", lambda: "auto")
    monkeypatch.setattr(bt, "_is_headed_mode", lambda: headed)
    monkeypatch.setattr(bt, "_blocked_private_page_action", lambda *a: None)
    monkeypatch.setattr(bt, "_get_session_info", lambda task_id=None: info)

    def _popen(argv, stdout=None, env=None, **_kw):
        spawned.append({"argv": argv, "env": env})
        os.write(stdout, json.dumps({"success": True, "data": {"secret": _SECRET}}).encode())
        return _Proc(on_wait)

    monkeypatch.setattr(bt.subprocess, "Popen", _popen)
    return spawned


def test_human_lease_refuses_the_command_before_anything_spawns(monkeypatch):
    spawned = _wire(monkeypatch)
    lease.acquire("human-viewer")
    result = json.loads(bt.browser_click("e1", task_id="t"))
    assert spawned == [], "human holds the lease, yet agent-browser was spawned"
    assert result["success"] is False and result["code"] == "human_has_control"

    lease.release("human-viewer")
    assert json.loads(bt.browser_click("e1", task_id="t"))["success"] is True
    assert len(spawned) == 1


def test_a_human_lease_with_the_screen_gone_still_fences_a_local_browser(monkeypatch):
    spawned = _wire(monkeypatch)
    monkeypatch.setattr(runtime, "published_env", lambda: {})
    lease.acquire("human-viewer")
    assert json.loads(bt.browser_click("e1", task_id="t"))["code"] == "human_has_control"
    assert spawned == []


def test_cloud_sessions_are_another_browser_and_run_unfenced(monkeypatch):
    spawned = _wire(monkeypatch, session={"session_name": "c", "bb_session_id": "bb",
                                          "cdp_url": "wss://cloud/x", "features": {}})
    monkeypatch.setattr(bt, "_ensure_cdp_supervisor", lambda task_id: None)
    lease.acquire("human-viewer")
    assert json.loads(bt.browser_click("e1", task_id="t"))["success"] is True
    assert len(spawned) == 1


def test_a_result_that_crossed_a_takeover_is_voided(monkeypatch):
    def takeover_and_handback():
        lease.acquire("human-viewer")
        lease.release("human-viewer")  # control is back, but the frame was the human's

    _wire(monkeypatch, on_wait=takeover_and_handback)
    raw = bt._run_browser_command("t", "snapshot", [])
    assert _SECRET not in json.dumps(raw)
    assert raw["success"] is False and raw["code"] == "human_has_control"


def test_console_eval_supervisor_fast_path_is_fenced(monkeypatch):
    import tools.browser_supervisor as supervisor_mod

    spawned = _wire(monkeypatch)
    bt._active_sessions["t"] = {"session_name": "h_bot", "cdp_url": None, "features": {"local": True}}
    evaluated: list = []

    class _Sup:
        def evaluate_runtime(self, expression, **_kw):
            evaluated.append(expression)
            return {"ok": True, "result": _SECRET}

    monkeypatch.setattr(supervisor_mod.SUPERVISOR_REGISTRY, "get", lambda task_id: _Sup())
    monkeypatch.setattr(bt, "_eval_ssrf_guard_active", lambda *a: False)
    lease.acquire("human-viewer")
    raw = bt._browser_eval("document.title", task_id="t")
    assert evaluated == [] and spawned == [] and _SECRET not in raw
    assert json.loads(raw)["code"] == "human_has_control"

    lease.release("human-viewer")
    assert json.loads(bt._browser_eval("document.title", task_id="t"))["result"] == _SECRET


def test_janitor_keeps_the_browser_a_human_holds_past_the_inactivity_timeout(monkeypatch):
    clock = {"now": 10_000.0}
    monkeypatch.setattr(bt.time, "time", lambda: clock["now"])
    monkeypatch.setattr(bt, "BROWSER_SESSION_INACTIVITY_TIMEOUT", 300)
    reaped: list = []
    monkeypatch.setattr(bt, "cleanup_browser", lambda task_id: reaped.append(task_id))
    bt._active_sessions.update({
        "bot": {"session_name": "h_bot", "features": {"local": True}},
        "cloud": {"session_name": "c_1", "bb_session_id": "bb", "features": {}},
    })
    bt._session_last_activity.update({"bot": 0.0, "cloud": 0.0})

    lease.acquire("human-viewer")
    bt._cleanup_inactive_browser_sessions()
    assert reaped == ["cloud"], "only the browser the human is not typing into may be reaped"
    assert bt._session_last_activity["bot"] == clock["now"], "the lease counts as activity"

    clock["now"] += 301  # still held, another full timeout later
    bt._cleanup_inactive_browser_sessions()
    assert reaped == ["cloud"]

    lease.release("human-viewer")
    clock["now"] += 301
    bt._cleanup_inactive_browser_sessions()
    assert reaped == ["cloud", "bot"], "handed back and idle: reaped as before"


def test_daemon_idle_timer_is_a_day_only_while_a_screen_is_published(monkeypatch):
    spawned = _wire(monkeypatch)
    monkeypatch.setattr(bt, "BROWSER_SESSION_INACTIVITY_TIMEOUT", 120)
    bt._run_browser_command("t", "snapshot", [])
    assert spawned[-1]["env"]["AGENT_BROWSER_IDLE_TIMEOUT_MS"] == "86400000"

    monkeypatch.setattr(runtime, "published_env", lambda: {})
    bt._run_browser_command("t", "snapshot", [])
    assert spawned[-1]["env"]["AGENT_BROWSER_IDLE_TIMEOUT_MS"] == "120000"

    monkeypatch.setattr(runtime, "published_env", lambda: {"DISPLAY": ":37"})
    monkeypatch.setattr(bt, "_is_headed_mode", lambda: False)  # headless browsing keeps the mirror
    bt._run_browser_command("t", "snapshot", [])
    assert spawned[-1]["env"]["AGENT_BROWSER_IDLE_TIMEOUT_MS"] == "120000"


@pytest.mark.parametrize("engine, headed, starts", [("auto", True, 1), ("auto", False, 0), ("lightpanda", True, 0)])
def test_headed_chromium_spawn_starts_the_screen_but_the_env_builder_never_does(monkeypatch, engine, headed, starts):
    spawned = _wire(monkeypatch, headed=headed)
    monkeypatch.setattr(bt, "_get_browser_engine", lambda: engine)
    monkeypatch.setattr(bt, "_lightpanda_fallback_reason", lambda *a: None)
    calls: list = []
    monkeypatch.setattr(runtime, "ensure_started_for_tool", lambda: calls.append(1))
    bt._build_browser_env()
    assert calls == []
    bt._run_browser_command("t", "snapshot", [])
    assert len(calls) == starts and len(spawned) == 1


def test_browser_env_carries_the_published_screen(monkeypatch):
    monkeypatch.setattr(runtime, "published_env", lambda: {"DISPLAY": ":37", "XAUTHORITY": "/x/auth"})
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    env = bt._build_browser_env()
    assert env["DISPLAY"] == ":37" and env["XAUTHORITY"] == "/x/auth" and "WAYLAND_DISPLAY" not in env
    monkeypatch.setattr(runtime, "published_env", lambda: {})
    assert bt._build_browser_env().get("DISPLAY") != ":37"


def test_vault_page_operations_respect_the_human_lease(monkeypatch):
    """Upstream ee5ee84a test_bot_desktop_browser_fence.py: the vault tools reach the page over the supervisor
    socket, outside `_run_browser_command`; while a human holds the screen they are refused and never touch it."""
    from tools import browser_vault_tool as vault

    bt._active_sessions["default"] = {"session_name": "review", "cdp_url": None, "features": {"local": True}}
    touched: list = []
    for name in ("browser_vault_fill", "browser_vault_enter_code", "browser_vault_save_login"):
        monkeypatch.setattr(vault, name, lambda *a, _n=name, **k: touched.append(_n) or json.dumps({"success": True}))
    lease.acquire("human")
    for handler in (vault._handle_vault_fill, vault._handle_vault_enter_code, vault._handle_vault_save_login):
        res = json.loads(handler({"handle": "vault_x"}, task_id="default"))
        assert res["code"] == "human_has_control", handler.__name__
    assert touched == [], "no vault page access while the human holds the screen"
    lease.release("human")
    assert json.loads(vault._handle_vault_fill({"handle": "vault_x"}, task_id="default"))["success"] is True


def test_secret_write_re_admits_after_a_takeover_during_the_code_prompt(monkeypatch):
    """Upstream ee5ee84a: a takeover while enter_code waits for the user's code refuses the write itself."""
    from tools import browser_vault_tool as vault

    bt._active_sessions["default"] = {"session_name": "review", "cdp_url": None, "features": {"local": True}}
    evaluated: list = []

    class _Sup:
        def evaluate_runtime(self, expr):
            evaluated.append(expr)
            return {"ok": True, "result": "{}"}

    monkeypatch.setattr(vault, "_ensure_supervisor", lambda tid: _Sup())
    assert vault._eval_js_secret("default", "fill()")["success"] is True
    lease.acquire("human")
    res = vault._eval_js_secret("default", "fill()")
    assert res["success"] is False and res["error_type"] == "human_has_control"
    assert evaluated == ["fill()"], "the credential must not reach the page under a human lease"
