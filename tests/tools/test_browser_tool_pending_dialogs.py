"""Pending dialogs must surface without touching the blocked page JS thread."""

import json
from unittest.mock import Mock

import pytest

from agent import redact
from tools import browser_supervisor as bs, browser_tool as bt


@pytest.mark.parametrize("full", [False, True])
def test_pending_dialog_snapshot_skips_page_commands_and_redacts_state(monkeypatch, full):
    secret = "pending-dialog-vault-canary"
    supervisor = bs.CDPSupervisor("task::local", "ws://example.invalid/cdp")
    supervisor._active = True
    supervisor._pending_dialogs["d-1"] = bs.PendingDialog(
        id="d-1", type="prompt", message=f"Echo {secret}", default_prompt=secret,
        opened_at=1.0, cdp_session_id="page-session",
    )
    get_supervisor = Mock(return_value=supervisor)
    command = Mock(side_effect=AssertionError("must not execute page commands while a dialog is pending"))
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
    monkeypatch.setitem(bt._last_active_session_key, "task", "task::local")
    monkeypatch.setitem(bt._active_sessions, "task::local", {"owner_task_id": "task", "session_key": "task::local"})
    monkeypatch.setattr(bs.SUPERVISOR_REGISTRY, "get", get_supervisor)
    monkeypatch.setattr(bt, "_run_browser_command", command)
    redact.register_vault_redaction_value(secret)
    try:
        raw = bt.browser_snapshot(full=full, task_id="task")
        out = json.loads(raw)
        assert out["success"] is True
        assert out["snapshot"] == "" and out["element_count"] == 0
        dialog = out["pending_dialogs"][0]
        assert dialog["id"] == "d-1" and dialog["type"] == "prompt"
        assert "«redacted-vault-secret»" in dialog["message"]
        assert dialog["default_prompt"] == "«redacted-vault-secret»"
        assert "frame_tree" in out and secret not in raw
        assert supervisor.snapshot().active
        get_supervisor.assert_called_once_with("task::local")
        command.assert_not_called()
    finally:
        redact.clear_vault_redaction_values()


@pytest.mark.parametrize("state", ["absent", "inactive", "no-dialog", "error"])
def test_without_an_active_pending_dialog_snapshot_keeps_ax_path(monkeypatch, state):
    supervisor = bs.CDPSupervisor("task", "ws://example.invalid/cdp")
    supervisor._active = state != "inactive"
    if state == "inactive":
        supervisor._pending_dialogs["d-1"] = bs.PendingDialog(
            id="d-1", type="alert", message="stale", default_prompt="", opened_at=1.0, cdp_session_id="page-session",
        )
    get_supervisor = Mock(return_value=None if state == "absent" else supervisor)
    if state == "error":
        get_supervisor.side_effect = RuntimeError("unavailable")
    command = Mock(return_value={"success": True, "data": {"snapshot": "AX page", "refs": {"e1": {}}}})
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(bt, "_allow_private_urls", lambda: True)
    monkeypatch.setattr(bs.SUPERVISOR_REGISTRY, "get", get_supervisor)
    monkeypatch.setattr(bt, "_run_browser_command", command)
    out = json.loads(bt.browser_snapshot(task_id="task"))
    assert out["success"] is True
    assert out["snapshot"] == "AX page" and out["element_count"] == 1
    command.assert_called_once_with("task", "snapshot", ["-c"])


@pytest.mark.parametrize("takeover", ["before", "during"])
def test_pending_dialog_snapshot_stays_inside_the_human_lease_fence(monkeypatch, takeover):
    from tools.bot_desktop import lease, runtime

    supervisor = bs.CDPSupervisor("task", "ws://example.invalid/cdp")
    supervisor._active = True
    supervisor._pending_dialogs["d-1"] = bs.PendingDialog(
        id="d-1", type="alert", message="human-private-dialog", default_prompt="",
        opened_at=1.0, cdp_session_id="page-session",
    )
    get_supervisor = Mock(return_value=supervisor)
    command = Mock(side_effect=AssertionError("must not execute page commands"))
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
    monkeypatch.setitem(bt._active_sessions, "task", {"features": {"local": True}})
    monkeypatch.setattr(runtime, "published_env", lambda: {"DISPLAY": ":37"})
    monkeypatch.setattr(bs.SUPERVISOR_REGISTRY, "get", get_supervisor)
    monkeypatch.setattr(bt, "_run_browser_command", command)
    real_snapshot = supervisor.snapshot

    def snapshot_with_takeover():
        state = real_snapshot()
        lease.acquire("viewer")
        lease.release("viewer")
        return state

    if takeover == "before":
        lease.acquire("viewer")
    else:
        monkeypatch.setattr(supervisor, "snapshot", snapshot_with_takeover)
    try:
        raw = bt.browser_snapshot(task_id="task")
        assert json.loads(raw)["code"] == "human_has_control"
        assert "human-private-dialog" not in raw
        command.assert_not_called()
        if takeover == "before":
            get_supervisor.assert_not_called()
        else:
            get_supervisor.assert_called_once_with("task")
    finally:
        lease.release("viewer")
