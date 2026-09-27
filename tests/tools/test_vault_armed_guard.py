"""V-9d guard unit: registry.dispatch refuses page-reading browser tools while a tab of the task is vault-armed.

Hermetic twin of tests/tools/test_vault_armed_tab_eval.py (real Chrome, opt-in): stub handlers, arming state set the
way ``CDPSupervisor.arm_vault`` sets it, every model-reachable browser tool name through the real ``dispatch``.
"""

import asyncio
import json
import threading

import pytest

from tools import browser_supervisor as bs
from tools.registry import ToolRegistry

READERS = [
    ("browser_console", {"expression": "pw.value"}),
    ("browser_vision", {"question": "?"}),
    ("browser_exec", {"code": "print(js('pw.value'))"}),
    ("browser_cdp", {"method": "Runtime.evaluate", "params": {"expression": "1"}}),
    ("browser_cdp", {"method": "Page.navigate", "params": {"url": "javascript:alert(pw.value)"}}),
    ("browser_cdp", {"method": "Page.navigate", "params": {"url": " JavaScript:1"}}),
]
ALLOWED = [
    ("browser_console", {}),
    ("browser_snapshot", {}),
    ("browser_click", {"ref": "@e1"}),
    ("browser_type", {"ref": "@e1", "text": "x"}),
    ("browser_navigate", {"url": "https://example.com/"}),
    ("browser_cdp", {"method": "Target.getTargets"}),
    ("browser_cdp", {"method": "Page.navigate", "params": {"url": "https://example.com/"}}),
]


@pytest.fixture
def shared(monkeypatch):
    """Which browser the tools drive: none shared by default (a per-task agent-browser session)."""
    from tools import browser_tool

    mode = {"cdp": "", "real_profile": False}
    monkeypatch.setattr(browser_tool, "_get_cdp_override_raw", lambda: mode["cdp"])
    monkeypatch.setattr(browser_tool, "_use_real_profile", lambda: mode["real_profile"])
    return mode


@pytest.fixture
def reg(monkeypatch, shared):
    monkeypatch.setattr(bs, "_VAULT_ARMED", {})
    r = ToolRegistry()
    for name in {n for n, _ in READERS + ALLOWED}:
        r.register(name=name, toolset="browser", schema={"name": name, "parameters": {"type": "object"}},
                   handler=lambda args, **kw: json.dumps({"success": True, "ran": True}))
    return r


def _arm(session_key, tab="T1", loader="L1"):
    bs._VAULT_ARMED.setdefault(session_key, {})[tab] = [loader, {loader}]


def _run(reg, name, args, task_id="t1"):
    return json.loads(reg.dispatch(name, args, task_id=task_id))


@pytest.mark.parametrize("name,args", READERS)
def test_readers_are_refused_while_armed_and_run_once_disarmed(reg, name, args):
    assert _run(reg, name, args).get("ran") is True  # not armed: runs
    _arm("t1")
    out = _run(reg, name, args)
    assert out["error_type"] == "vault_armed" and out["success"] is False and "ran" not in out
    bs._VAULT_ARMED["t1"]["T1"][0] = "L2"  # the main frame committed a new document
    assert _run(reg, name, args).get("ran") is True


@pytest.mark.parametrize("name,args", ALLOWED)
def test_page_blind_tools_run_on_an_armed_tab(reg, name, args):
    _arm("t1")
    assert _run(reg, name, args).get("ran") is True


def test_a_local_sidecar_tab_arms_the_task_and_other_tasks_are_untouched(reg):
    _arm("t1::local")
    assert _run(reg, "browser_console", {"expression": "1"})["error_type"] == "vault_armed"
    assert _run(reg, "browser_console", {"expression": "1"}, task_id="t2").get("ran") is True
    bs._vault_forget("t1::local", "T1")  # the tab closed
    assert _run(reg, "browser_console", {"expression": "1"}).get("ran") is True


@pytest.mark.parametrize("mode", [{"cdp": "ws://127.0.0.1:9222/devtools/browser/x"}, {"real_profile": True}])
@pytest.mark.parametrize("name,args", READERS + [
    ("browser_cdp", {"method": "Runtime.evaluate", "target_id": "T1", "params": {"expression": "1"}})])
def test_a_browser_every_task_shares_is_armed_for_every_task(reg, shared, mode, name, args):
    """A CDP override or the real-profile browser is one browser: another task id reaches the armed tab too."""
    shared.update(mode)
    _arm("t1")
    for task_id in ("t2", None):
        out = _run(reg, name, args, task_id=task_id)
        assert out.get("error_type") == "vault_armed" and "ran" not in out, (task_id, out)
    bs._VAULT_ARMED["t1"]["T1"][0] = "L2"  # the tab loaded a new document
    assert _run(reg, name, args, task_id="t2").get("ran") is True


@pytest.fixture
def sessions(monkeypatch):
    """The browser tool's cached sessions (session key -> session_info), empty by default."""
    from tools import browser_tool

    cache = {}
    monkeypatch.setattr(browser_tool, "_active_sessions", cache)
    return cache


def _boom():
    raise OSError("config unreadable")


@pytest.mark.parametrize("session_key", ["t2", "t2::local"])
@pytest.mark.parametrize("config", ["off", "raises"])
def test_a_cached_session_on_the_real_profile_browser_stays_shared_whatever_the_config(
        reg, shared, sessions, monkeypatch, session_key, config):
    """The config no longer selects the real-profile browser (or cannot be read), but t2's cached session still
    drives it: t1's armed tab there is reachable from t2 (lead ruling r2, decision 1b)."""
    from tools import browser_tool

    sessions[session_key] = {"features": {"local": True, "real_profile": True}}
    if config == "raises":
        monkeypatch.setattr(browser_tool, "_use_real_profile", _boom)
    _arm("t1")
    out = _run(reg, "browser_console", {"expression": "pw.value"}, task_id="t2")
    assert out.get("error_type") == "vault_armed" and "ran" not in out, out
    del sessions[session_key]  # a task with no session on a shared browser keeps its own scope
    if config == "off":
        assert _run(reg, "browser_console", {"expression": "1"}, task_id="t2").get("ran") is True


def test_a_failing_session_lookup_fails_closed(reg, sessions, monkeypatch):
    from tools import browser_tool

    class Broken:
        def get(self, key):
            raise RuntimeError("lookup failed")

    _arm("t1")
    monkeypatch.setattr(browser_tool, "_active_sessions", Broken())
    out = _run(reg, "browser_vision", {"question": "?"}, task_id="t2")
    assert out.get("error_type") == "vault_armed" and "ran" not in out, out


def _cleanup(session_key, sessions, monkeypatch):
    from tools import browser_tool

    monkeypatch.setattr(browser_tool, "_stop_cdp_supervisor", lambda k: None)
    monkeypatch.setattr(browser_tool, "_maybe_stop_recording", lambda k: None)
    monkeypatch.setattr(browser_tool, "_run_browser_command", lambda *a, **k: {"success": True})
    browser_tool._cleanup_single_browser_session(session_key)
    assert session_key not in sessions


def test_retiring_a_session_whose_browser_dies_with_it_disarms_its_tabs(reg, shared, sessions, monkeypatch):
    sessions["t1"] = {"session_name": "", "bb_session_id": None, "features": {"local": True}}
    sessions["t1::local"] = {"session_name": "", "bb_session_id": None, "features": {"local": True}}
    _arm("t1")
    _arm("t1::local", tab="T2")
    _cleanup("t1", sessions, monkeypatch)
    assert bs.vault_armed("t1")  # the sidecar is still alive and may hold the tab
    _cleanup("t1::local", sessions, monkeypatch)
    assert not bs.vault_armed("t1") and not bs.vault_armed(None)
    shared["cdp"] = "ws://127.0.0.1:9222/devtools/browser/x"  # another task on a shared browser is not refused
    assert _run(reg, "browser_console", {"expression": "1"}, task_id="t2").get("ran") is True


@pytest.mark.parametrize("features", [{"cdp_override": True}, {"local": True, "real_profile": True}])
def test_retiring_a_session_on_a_shared_browser_keeps_its_tabs_armed(reg, shared, sessions, monkeypatch, features):
    """The shared browser outlives the session, and the filled tab with it (lead ruling r2, decision 2)."""
    sessions["t1"] = {"session_name": "", "bb_session_id": None, "features": features}
    _arm("t1")
    _cleanup("t1", sessions, monkeypatch)
    shared["cdp"] = "ws://127.0.0.1:9222/devtools/browser/x"
    out = _run(reg, "browser_console", {"expression": "1"}, task_id="t2")
    assert out.get("error_type") == "vault_armed" and "ran" not in out, out


@pytest.fixture
def sup(monkeypatch):
    """A real CDPSupervisor on a real loop; only the CDP wire is stubbed (the frame tree the tab shows now)."""
    monkeypatch.setattr(bs, "_VAULT_ARMED", {})
    s = bs.CDPSupervisor("t1", "ws://x")
    s._page_session_id, s._active, s.frame = "PAGE", True, {"id": "T1", "loaderId": "L1"}

    async def cdp(method, params=None, session_id=None, **kwargs):
        assert (method, session_id) == ("Page.getFrameTree", "PAGE")
        return {"result": {"frameTree": {"frame": dict(s.frame)}}}

    s._cdp = cdp
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    s._loop = loop
    try:
        yield s
    finally:
        loop.call_soon_threadsafe(loop.stop)


def _event(sup, method, params):
    asyncio.run_coroutine_threadsafe(sup._on_event(method, params, "PAGE"), sup._loop).result(5)


def test_the_tab_is_armed_per_document(sup):
    sup.arm_vault()
    assert bs.vault_armed("t1", "T1") and bs.vault_armed("t1")
    _event(sup, "Page.navigatedWithinDocument", {"frameId": "T1", "url": "http://a.test/#x"})
    assert bs.vault_armed("t1")  # hash / pushState: same document, still armed
    _event(sup, "Page.frameNavigated", {"frame": {"id": "IFRAME", "parentId": "T1", "loaderId": "L9"}})
    assert bs.vault_armed("t1")  # a subframe document does not replace the tab's
    _event(sup, "Page.frameNavigated", {"frame": {"id": "T1", "loaderId": "L2"}})
    assert not bs.vault_armed("t1")  # the main frame committed a new document
    _event(sup, "Page.frameNavigated", {"frame": {"id": "T1", "loaderId": "L1"}})
    assert bs.vault_armed("t1")  # back/forward cache restored the filled document
    _event(sup, "Target.targetDestroyed", {"targetId": "T1"})
    assert not bs.vault_armed("t1") and bs._VAULT_ARMED["t1"] == {}  # the tab closed: its state is gone


def test_arming_fails_closed_without_a_page_session(sup):
    sup._page_session_id = None
    with pytest.raises(RuntimeError):
        sup.arm_vault()
    assert not bs.vault_armed("t1")


def test_the_fill_is_refused_when_the_tab_cannot_be_armed(monkeypatch):
    from tools import browser_vault_tool as bvt

    wrote = []

    class Sup:
        def arm_vault(self):
            raise RuntimeError("no page session")

        def evaluate_runtime(self, expression):
            wrote.append(expression)
            return {"ok": True}

    monkeypatch.setattr(bvt, "_ensure_supervisor", lambda task_id: Sup())
    monkeypatch.setattr(bvt, "_bot_desktop_browser_session", lambda task_id: False)
    out = bvt._eval_js_secret("t1", "fill('SECRET')")
    assert out["error_type"] == "vault_arm_failed" and wrote == []
