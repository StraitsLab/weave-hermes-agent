"""Vault custody must block transformed reads, including shared-browser sibling tasks."""

import json
from unittest.mock import Mock, patch

import pytest

from tools import harso_vault_guard as guard


@pytest.fixture(autouse=True)
def empty_guard():
    with guard._LOCK:
        guard._TABS.clear()
    yield
    with guard._LOCK:
        guard._TABS.clear()


def test_fill_arms_before_secret_evaluation():
    from tools import browser_vault_tool as vault

    supervisor = Mock()
    supervisor.evaluate_runtime.return_value = {"ok": True, "result": "filled"}
    events = []
    supervisor.arm_vault.side_effect = lambda: events.append("arm")
    supervisor.evaluate_runtime.side_effect = lambda expr: events.append("write") or {"ok": True}
    with patch.object(vault, "_ensure_supervisor", return_value=supervisor), \
         patch.object(vault, "_bot_desktop_browser_session", return_value=False):
        assert vault._eval_js_secret("task", "secret expression")["success"]
    assert events == ["arm", "write"]


def test_failed_arm_never_writes():
    from tools import browser_vault_tool as vault

    supervisor = Mock()
    supervisor.arm_vault.side_effect = RuntimeError("unavailable")
    with patch.object(vault, "_ensure_supervisor", return_value=supervisor), \
         patch.object(vault, "_bot_desktop_browser_session", return_value=False):
        assert not vault._eval_js_secret("task", "secret expression")["success"]
    supervisor.evaluate_runtime.assert_not_called()


@pytest.mark.parametrize("name,args", [
    ("browser_exec", {"code": "js('btoa(document.querySelector(\"input\").value)')"}),
    ("browser_vision", {}),
    ("browser_console", {"expression": "secret.split('').reverse().join('')"}),
    ("browser_cdp", {"method": "Page.captureScreenshot"}),
    ("browser_cdp", {"method": "Page.navigate", "params": {"url": "javascript:alert(1)"}}),
])
def test_execution_middleware_refuses_before_downstream(name, args):
    guard._TABS[("task", "endpoint", "tab")] = ["filled", {"filled"}]
    downstream = Mock()
    with patch.object(guard, "_shared", return_value=False):
        result = guard.protect(tool_name=name, args=args, task_id="task::local", next_call=downstream)
    assert json.loads(result)["error_type"] == "vault_armed"
    downstream.assert_not_called()


def test_shared_browser_reaches_other_task_but_isolated_browser_does_not():
    guard._TABS[("first", "endpoint", "tab")] = ["filled", {"filled"}]
    downstream = Mock(return_value="safe")
    with patch.object(guard, "_shared", return_value=True):
        assert json.loads(guard.protect(tool_name="browser_exec", args={}, task_id="second",
                                       next_call=downstream))["error_type"] == "vault_armed"
    with patch.object(guard, "_shared", return_value=False):
        assert guard.protect(tool_name="browser_exec", args={}, task_id="second", next_call=downstream) == "safe"


@pytest.mark.parametrize("name,args", [
    ("browser_snapshot", {}), ("browser_console", {}),
    ("browser_cdp", {"method": "Target.getTargets"}),
    ("browser_cdp", {"method": "Page.navigate", "params": {"url": "https://example.test"}}),
])
def test_page_blind_operations_remain_available(name, args):
    guard._TABS[("task", "endpoint", "tab")] = ["filled", {"filled"}]
    with patch.object(guard, "_shared", return_value=True):
        assert guard.protect(tool_name=name, args=args, task_id="task", next_call=lambda args: "ok") == "ok"


def test_navigation_and_bfcache_only_update_same_endpoint():
    supervisor = Mock(task_id="task", cdp_url="endpoint")
    guard._TABS[("task", "endpoint", "tab")] = ["filled", {"filled"}]
    guard.observe_frame(supervisor, {"frame": {"id": "tab", "loaderId": "new"}})
    assert not guard.armed("task")
    guard.observe_frame(Mock(task_id="task", cdp_url="other"), {"frame": {"id": "tab", "loaderId": "filled"}})
    assert not guard.armed("task")
    guard.observe_frame(supervisor, {"frame": {"id": "tab", "loaderId": "filled"}})
    assert guard.armed("task")


def test_plugin_registers_short_circuit_middleware():
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    spec = spec_from_file_location("guard_plugin", Path(__file__).resolve().parents[2] / "plugins/harso-vault-guard/__init__.py")
    plugin = module_from_spec(spec)
    spec.loader.exec_module(plugin)
    ctx = Mock()
    plugin.register(ctx)
    ctx.register_middleware.assert_called_once_with("tool_execution", guard.protect)


def test_real_plugin_discovery_and_execution_chain(tmp_path, monkeypatch):
    from hermes_cli import plugins
    from hermes_cli.middleware import run_tool_execution_middleware

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("plugins:\n  enabled: [harso-vault-guard]\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = plugins.PluginManager()
    manager.discover_and_load()
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    assert guard.protect in manager._middleware["tool_execution"]
    guard._TABS[("first", "endpoint", "tab")] = ["filled", {"filled"}]
    downstream = Mock()
    with patch.object(guard, "_shared", return_value=True):
        result = run_tool_execution_middleware("browser_exec", {"code": "capture_screenshot()"},
                                               downstream, task_id="second")
    assert json.loads(result)["error_type"] == "vault_armed"
    downstream.assert_not_called()


def test_native_arm_requires_active_middleware():
    from hermes_cli import plugins

    manager = Mock(_middleware={})
    supervisor = Mock()
    with patch.object(plugins, "_delivery_manager", return_value=manager):
        with pytest.raises(RuntimeError, match="Enable"):
            guard.arm(supervisor)
    supervisor._cdp.assert_not_called()


def test_shared_cached_browser_survives_config_change():
    from tools import browser_tool as bt

    with patch("hermes_cli.config.read_raw_config", return_value={}), \
         patch.dict(bt._active_sessions, {"task::local": {"features": {"real_profile": True}}}, clear=True):
        assert guard._shared("task")


def test_unknown_reachability_fails_closed():
    guard._TABS[("first", "endpoint", "tab")] = ["filled", {"filled"}]
    downstream = Mock()
    with patch.object(guard, "_shared", side_effect=RuntimeError("unknown")):
        assert json.loads(guard.protect(tool_name="browser_exec", args={}, task_id="second",
                                       next_call=downstream))["error_type"] == "vault_armed"
    downstream.assert_not_called()


def test_missing_loader_and_same_document_event_keep_custody():
    from tools.browser_supervisor import CDPSupervisor

    supervisor = CDPSupervisor("task", "endpoint")
    guard._TABS[("task", "endpoint", "tab")] = ["filled", {"filled"}]
    supervisor._EVENT_HANDLERS["Page.frameNavigated"](supervisor, {"frame": {"id": "tab"}}, "session")
    assert guard.armed("task")
    assert "Page.navigatedWithinDocument" not in supervisor._EVENT_HANDLERS
