"""Unattended API requests cannot inherit a CLI approval prompt."""
from tools import approval
import pytest


@pytest.mark.parametrize("ask", ["0", "1"])
def test_unattended_api_clears_inherited_cli(monkeypatch, ask):
    monkeypatch.setenv("HERMES_EXEC_ASK", ask)
    monkeypatch.setattr(approval, "_is_interactive_cli", lambda: True)
    monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: False)
    monkeypatch.setattr(approval, "_is_single_query_approval_context", lambda: False)
    monkeypatch.setattr(approval, "_is_cron_approval_context", lambda: False)
    monkeypatch.setattr(approval, "_is_unattended_platform_approval_context", lambda: True)
    monkeypatch.setattr(approval, "_get_session_platform", lambda: "api_server")
    monkeypatch.setattr(approval, "_gateway_notify_cb", lambda key: None)
    assert approval._presence()[1:] == (False, False, False)


def test_api_with_live_notifier_keeps_approval_surface(monkeypatch):
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.setattr(approval, "_is_interactive_cli", lambda: False)
    monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: False)
    monkeypatch.setattr(approval, "_is_single_query_approval_context", lambda: False)
    monkeypatch.setattr(approval, "_is_cron_approval_context", lambda: False)
    monkeypatch.setattr(approval, "_is_unattended_platform_approval_context", lambda: True)
    monkeypatch.setattr(approval, "_get_session_platform", lambda: "api_server")
    monkeypatch.setattr(approval, "_gateway_notify_cb", lambda key: object())
    assert approval._presence()[1:] == (False, False, True)
