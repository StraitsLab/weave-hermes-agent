"""DEC-450: the approval channel carries masked answers in memory, never in its projection/hooks."""
import json

import pytest

from tools import approval as ap, approval_context


@pytest.fixture(autouse=True)
def attended(monkeypatch):
    monkeypatch.setattr(approval_context, "_no_user_can_answer", lambda: False)


@pytest.mark.parametrize("kind,secret", [("vault_code", {"code": "synthetic-code-canary"}),
                                        ("vault_save_login", {"username": "test@example.com", "password": "synthetic-password-canary"})])
def test_secret_uses_waiter_only_and_never_hooks_or_pending(monkeypatch, caplog, kind, secret):
    session = "vault-secret-test"
    hooks = []
    monkeypatch.setattr(approval_context, "_fire_approval_hook", lambda *a, **kw: hooks.append(kw))
    def notify(data):
        assert data["kind"] == kind
        assert "secret" not in json.dumps(ap.list_gateway_approvals(session))
        assert ap.resolve_gateway_approval(session, "once", request_id=data["request_id"], secret=secret) == 1
    token = approval_context.set_current_session_key(session)
    ap.register_gateway_notify(session, notify)
    try:
        result = ap.request_vault_secret(kind, "example.com")
        assert result == secret
        assert not ap.list_gateway_approvals(session)
        for value in secret.values():
            assert value not in json.dumps(hooks) + caplog.text
    finally:
        ap.unregister_gateway_notify(session)
        approval_context.reset_current_session_key(token)


def test_cancel_timeout_and_missing_surface_release_no_secret(monkeypatch):
    session = "vault-secret-cancel"
    token = approval_context.set_current_session_key(session)
    try:
        assert ap.request_vault_secret("vault_code", "example.com") is None
        monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: 0)
        ap.register_gateway_notify(session, lambda _: None)
        assert ap.request_vault_secret("vault_code", "example.com") is None
        assert not ap.list_gateway_approvals(session)
    finally:
        ap.unregister_gateway_notify(session)
        approval_context.reset_current_session_key(token)


@pytest.mark.parametrize("fails", [False, True])
def test_native_runner_binds_masked_callbacks_and_clears_on_exit(monkeypatch, fails):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from gateway.config import Platform
    from gateway.turn_context import TurnContext
    from gateway.run_turn_runner import TurnRunner
    from agent.vault_backends import unlock
    import agent.notification_presentation as notifications
    import sys
    monkeypatch.setitem(sys.modules, "gateway.run", SimpleNamespace(_wrap_current_message_with_observed_context=lambda message, context: message))
    ctx = TurnContext()
    ctx.source = SimpleNamespace(platform=Platform.API_SERVER, user_id="user", user_name="User")
    ctx.session_key = "masked-runner"; ctx.session_id = "session"
    worker = SimpleNamespace(_ctx=ctx, _approval_notify_sync=lambda _: None, _native_image_run_message=lambda: "Sign in",
                             _approval_owner=None)  # upstream (v0.22.5) arms the onboarding prelude from the owner
    monkeypatch.setattr(notifications, "notification_turn", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(ap, "request_vault_secret", lambda kind, site: {"code": "code-canary"} if kind == "vault_code" else {"username": "user", "password": "password-canary"})
    def run(message, **kwargs):
        assert message == "Sign in" and "canary" not in str(kwargs)
        assert unlock.get_code_prompt_callback()("example.com", "") == "code-canary"
        assert unlock.get_save_login_prompt_callback()("https://example.com", "example.com") == {"identifier": "user", "password": "password-canary"}
        if fails:
            raise RuntimeError("interrupted")
        return {"final_response": "Done"}
    try:
        if fails:
            with pytest.raises(RuntimeError, match="interrupted"):
                TurnRunner._run_conversation_with_approval(worker, SimpleNamespace(run_conversation=run), [], None, None, None)
        else:
            assert TurnRunner._run_conversation_with_approval(worker, SimpleNamespace(run_conversation=run), [], None, None, None) == {"final_response": "Done"}
        assert unlock.get_code_prompt_callback() is None and unlock.get_save_login_prompt_callback() is None
        assert "masked-runner" not in ap._gateway_notify_cbs
    finally:
        unlock.set_code_prompt_callback(None); unlock.set_save_login_prompt_callback(None)


def test_secret_rejected_for_normal_approval():
    from tools.approval_gateway_wait import _ApprovalEntry
    entry = _ApprovalEntry({"command": "ordinary"})
    ap._gateway_queues["normal-secret-test"] = [entry]
    try:
        assert ap.resolve_gateway_approval("normal-secret-test", "once", request_id=entry.data["request_id"], secret={"code": "canary"}) == 0
        assert not entry.event.is_set()
    finally:
        ap.unregister_gateway_notify("normal-secret-test")
