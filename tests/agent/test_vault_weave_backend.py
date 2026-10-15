"""Native Vault source boundary; trimmed/adapted from the pre-1030559b44 backend tests."""
import json
from contextlib import contextmanager
from unittest.mock import Mock, patch

import httpx
import pytest

from agent.vault_backends import base, weave
from agent.vault_backends.base import VaultUnavailable, VaultUseRefused
from gateway.native_bot_admission import native_turn_context

HANDLE = "wv:0199bbbb-0000-7000-8000-00000000000a"
RUN = "0199aaaa-0000-7000-8000-000000000001"
CARD = "0199dddd-0000-7000-8000-000000000001"
ORIGIN = "https://site.example"
PASSWORD = "native-vault-synthetic-canary"
BEARER = "wvc1_" + "A" * 43
META = dict(id=HANDLE, kind="login", label="Example", allowed_origins=[ORIGIN],
            identifier="user@example.com", identifier_type="email", has_otp=True, created_at="now")


@pytest.fixture
def source(monkeypatch):
    monkeypatch.setattr(base, "_cfg", lambda: {"weave": {"enabled": True, "base_url": "https://vault.example"}})
    monkeypatch.setenv("WEAVE_API_MCP_BEARER", BEARER)
    token = native_turn_context.set({"session_id": "session", "run_id": RUN})
    backend = weave.WeaveLoginBackend("https://vault.example")
    calls = []
    answer = {"decision": "always", "action": "fill_login", "password": PASSWORD}
    real_client = httpx.Client

    def server(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json=answer)
        if request.url.path.endswith("/items"):
            return httpx.Response(200, json={"items": [dict(META, password=PASSWORD), dict(META, kind="payment")]})
        return httpx.Response(200, json=META)

    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(server), **kw))
    yield backend, calls, answer
    native_turn_context.reset(token)


def test_config_selects_only_weave_and_never_constructs_local(source):
    with patch("agent.vault_backends.local.LocalLoginBackend", side_effect=AssertionError("local store opened")):
        sources = base.enabled_backends()
    assert [b.name for b in sources] == ["weave"]
    assert sources[0].can_save is False and sources[0].protects_all_values is True
    assert base.backend_for_handle("vault_old") is None


def test_list_metadata_and_resolve_exact_origin_over_cell_bearer(source):
    backend, calls, _ = source
    items = backend.list_items()
    assert len(items) == 1 and PASSWORD not in json.dumps(items[0].to_dict())
    assert backend.get_meta(HANDLE).allowed_origins == (ORIGIN,)
    assert backend.resolve_password(HANDLE, origin=ORIGIN) == PASSWORD
    request = calls[-1]
    assert json.loads(request.content) == dict(handle=HANDLE, action="fill_login", page_origin=ORIGIN,
                                             session_id="session", run_id=RUN)
    assert request.headers["Authorization"] == "Bearer " + BEARER


@pytest.mark.parametrize("settled", ["once", "always"])
def test_approval_waits_then_resolves_once_with_identical_body(source, monkeypatch, settled):
    backend, calls, answer = source
    answer.clear(); answer.update(decision="approval_required", approval_ref=CARD)
    def approve(*args, **kwargs):
        assert len(calls) == 1 and kwargs == {"rule_key": CARD}
        answer.clear(); answer.update(decision=settled, action="fill_login", password=PASSWORD)
        return {"approved": True}
    monkeypatch.setattr("tools.approval.request_tool_approval", approve)
    assert backend.resolve_password(HANDLE, origin=ORIGIN) == PASSWORD
    assert len(calls) == 2 and calls[0].content == calls[1].content


@pytest.mark.parametrize("approved", [False, None])
def test_denial_never_retries(source, monkeypatch, approved):
    backend, calls, answer = source
    answer.clear(); answer.update(decision="approval_required", approval_ref=CARD)
    monkeypatch.setattr("tools.approval.request_tool_approval", lambda *a, **kw: {"approved": approved})
    with pytest.raises(VaultUseRefused) as refused:
        backend.resolve_password(HANDLE, origin=ORIGIN)
    assert refused.value.error_type == "denied" and len(calls) == 1


def test_native_allow_without_settled_server_card_releases_nothing(source, monkeypatch):
    backend, calls, answer = source
    answer.clear(); answer.update(decision="approval_required", approval_ref=CARD)
    monkeypatch.setattr("tools.approval.request_tool_approval", lambda *a, **kw: {"approved": True})
    with pytest.raises(VaultUnavailable):
        backend.resolve_password(HANDLE, origin=ORIGIN)
    assert len(calls) == 2


@pytest.mark.parametrize("ref", [None, "not-a-card", "wv:" + CARD])
def test_invalid_approval_reference_never_reaches_gate(source, ref):
    backend, _, answer = source
    answer.clear(); answer.update(decision="approval_required", approval_ref=ref)
    with patch("tools.approval.request_tool_approval") as gate, pytest.raises(VaultUnavailable):
        backend.resolve_password(HANDLE, origin=ORIGIN)
    gate.assert_not_called()


def test_no_native_admission_fails_before_http(source):
    backend, calls, _ = source
    token = native_turn_context.set(None)
    try:
        with pytest.raises(VaultUseRefused) as refused:
            backend.resolve_password(HANDLE, origin=ORIGIN)
        assert refused.value.error_type == "no_run" and not calls
    finally:
        native_turn_context.reset(token)


@pytest.mark.parametrize("bearer", ["wva1_" + "A" * 43, "bad\nheader", " " + BEARER])
def test_malformed_or_work_bearer_never_reaches_transport(source, monkeypatch, bearer):
    backend, calls, _ = source
    monkeypatch.setenv("WEAVE_API_MCP_BEARER", bearer)
    with pytest.raises(VaultUnavailable) as error:
        backend.list_items()
    assert bearer not in str(error.value) and not calls


@pytest.mark.parametrize("response", [httpx.Response(302, headers={"Location": "https://other.example"}),
                                      httpx.Response(500, text=PASSWORD), httpx.Response(200, text=PASSWORD)])
def test_transport_failure_is_content_free(source, monkeypatch, response):
    backend, _, _ = source
    response.request = httpx.Request("GET", "https://vault.example")
    with patch("httpx.Client") as client:
        client.return_value.__enter__.return_value.request.return_value = response
        with pytest.raises(VaultUnavailable) as error:
            backend.list_items()
    assert PASSWORD not in str(error.value)


def test_otp_code_not_seed(source):
    backend, _, answer = source
    answer.clear(); answer.update(decision="once", action="enter_otp", otp="123456")
    assert backend.resolve_otp(HANDLE, origin=ORIGIN) == "123456"


@contextmanager
def page(backend):
    from tools import browser_vault_tool as tool
    supervisor = Mock()
    supervisor.evaluate_runtime.return_value = {"ok": True, "result": json.dumps({"filled": 1})}
    controls = [{"name": "password", "type": "password", "index": 0, "autocomplete": "current-password"},
                {"name": "code", "type": "text", "index": 1, "autocomplete": "one-time-code"}]
    with patch("agent.vault_backends.backend_for_handle", return_value=backend), \
         patch.object(tool, "_ensure_supervisor", return_value=supervisor), \
         patch.object(tool, "_focus_bound_origin", return_value=None), \
         patch.object(tool, "_current_page_origin", return_value=ORIGIN), \
         patch.object(tool, "_eval_js", return_value={"success": True, "result": json.dumps(controls)}), \
         patch("agent.redact.register_vault_redaction_value") as redact:
        yield supervisor, redact


def test_native_fill_passes_origin_redacts_and_arms_guard(source):
    from tools.browser_vault_tool import browser_vault_fill
    backend, _, _ = source
    with page(backend) as (supervisor, redact):
        raw = browser_vault_fill(HANDLE, task_id="task")
    assert json.loads(raw)["success"] is True
    assert PASSWORD not in raw and PASSWORD in supervisor.evaluate_runtime.call_args.args[0]
    redact.assert_called_once_with(PASSWORD)
    supervisor.arm_vault.assert_called_once()


def test_origin_mismatch_never_resolves(source):
    from tools.browser_vault_tool import browser_vault_fill
    backend, calls, _ = source
    with page(backend) as (supervisor, _), patch("tools.browser_vault_tool._current_page_origin", return_value="https://evil.example"):
        result = json.loads(browser_vault_fill(HANDLE))
    assert result["error_type"] == "origin_mismatch"
    assert all(c.method != "POST" for c in calls)
    supervisor.evaluate_runtime.assert_not_called()


def test_native_code_uses_origin_and_redacts(source):
    from tools.browser_vault_tool import browser_vault_enter_code
    backend, _, answer = source
    answer.clear(); answer.update(decision="once", action="enter_otp", otp="123456")
    with page(backend) as (supervisor, redact):
        result = json.loads(browser_vault_enter_code(HANDLE))
    assert result["success"] and result["source"] == "weave"
    redact.assert_called_once_with("123456")
    supervisor.arm_vault.assert_called_once()


def test_guard_failure_never_injects(source):
    from tools.browser_vault_tool import browser_vault_fill
    backend, _, _ = source
    with page(backend) as (supervisor, _):
        supervisor.arm_vault.side_effect = RuntimeError("plugin disabled")
        result = json.loads(browser_vault_fill(HANDLE))
    assert result["success"] is False
    supervisor.evaluate_runtime.assert_not_called()


def test_harso_save_never_opens_prompt_or_local_store(source):
    from tools.browser_vault_tool import browser_vault_save_login
    with patch("agent.vault_backends.unlock.get_save_login_prompt_callback") as bridge:
        result = json.loads(browser_vault_save_login())
    assert result["error_type"] == "save_in_app"
    bridge.assert_not_called()
