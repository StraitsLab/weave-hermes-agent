"""API session context outranks process-wide interactive approval flags."""
from contextvars import Context
import time
from unittest.mock import MagicMock

import pytest

from gateway.platforms.api_server import APIServerAdapter
from tools import approval as ap


@pytest.mark.parametrize("unattended", [None, "approve"])
@pytest.mark.parametrize("interactive", ["", "1"])
@pytest.mark.parametrize("mode", ["manual", "smart"])
@pytest.mark.parametrize("entry", ["command", "gate"])
def test_api_session_never_waits(monkeypatch, mode, entry, interactive, unattended):
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    for key in ("HERMES_EXEC_ASK", "HERMES_GATEWAY_SESSION", "HERMES_INTERACTIVE"):
        monkeypatch.setenv(key, "1")
    monkeypatch.setenv("HERMES_INTERACTIVE", interactive)
    monkeypatch.setattr(ap, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(ap, "is_current_session_yolo_enabled", lambda: False)
    monkeypatch.setattr(ap, "is_approved", lambda *a: False)
    monkeypatch.setattr(ap, "_command_matches_permanent_allowlist", lambda *a: False)
    config = {"mode": mode}
    if unattended:
        config["unattended_mode"] = unattended
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"approvals": config})
    monkeypatch.setattr(ap, "_get_approval_config", lambda: config)
    monkeypatch.setattr("tools.tirith_security.check_command_security", lambda *a: {"action": "allow"})
    judge = MagicMock(side_effect=AttributeError("credential has no strip"))
    wait = MagicMock(side_effect=AssertionError("human approval submitted"))
    monkeypatch.setattr("agent.auxiliary_client.call_llm", judge)
    monkeypatch.setattr(ap, "_await_gateway_decision", wait)
    monkeypatch.setitem(ap._gateway_notify_cbs, "api-test", lambda *a: None)

    def run():
        APIServerAdapter._bind_api_server_session(session_key="api-test")
        assert ap._get_session_platform() == "api_server"
        if mode == "smart":
            assert ap._smart_approve("sudo systemctl restart nginx", "sudo") == "escalate"
        start = time.monotonic()
        if entry == "command":
            result = ap.check_all_command_guards("sudo systemctl restart nginx", "local")
        else:
            result = ap._run_approval_gate(pattern_key="test", description="sudo",
                display_target="sudo systemctl restart nginx", cron_deny_message="cron",
                single_query_deny_message="single", autoapprove_log_prefix="test")
        assert time.monotonic() - start < 1
        assert result["approved"] is (unattended == "approve")
        if not result["approved"]:
            assert "unattended platform" in result["message"]
        wait.assert_not_called()
    Context().run(run)
