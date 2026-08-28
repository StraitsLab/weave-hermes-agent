from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading

import pytest

from tui_gateway import server


class _Transport:
    def __init__(self, trusted_controller: bool):
        self.trusted_controller = trusted_controller
        self.writes = []

    def write(self, value):
        self.writes.append(value)


class _Agent:
    model = "stock-model"
    provider = "stock-provider"
    base_url = "https://stock.example/v1"
    api_mode = "chat_completions"

    def __init__(self):
        self.calls = []

    def switch_model(self, **kwargs):
        self.calls.append(kwargs)


@pytest.fixture(autouse=True)
def _clear_live_sessions():
    server._sessions.clear()
    yield
    server._sessions.clear()


def _expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")


def _bind_params(**overrides):
    params = {
        "session_id": "native-sid",
        "credential_slot": "GATE_B_API_KEY",
        "bearer": "test-bearer-never-log",
        "expires_at": _expiry(),
        "provider_route_revision_id": "opaque-route-revision",
    }
    params.update(overrides)
    return params


def _session(agent=None, *, key="stored-key"):
    return {"agent": agent, "session_key": key}


def test_callable_credential_is_redacted_refreshable_and_expiring():
    holder = server._SessionCredential("first-bearer", datetime.now(timezone.utc) + timedelta(minutes=1))

    assert holder() == "first-bearer"
    assert "first-bearer" not in repr(holder)
    holder.refresh("second-bearer", datetime.now(timezone.utc) + timedelta(minutes=1))
    assert holder() == "second-bearer"
    holder.revoke()
    with pytest.raises(RuntimeError, match="credential unavailable"):
        holder()
    assert holder.refresh(
        "revoked-replacement", datetime.now(timezone.utc) + timedelta(minutes=1)
    ) is False

    expired = server._SessionCredential("expired-bearer", datetime.now(timezone.utc) - timedelta(seconds=1))
    with pytest.raises(RuntimeError, match="credential unavailable"):
        expired()
    assert expired.refresh(
        "replacement-bearer", datetime.now(timezone.utc) + timedelta(minutes=1)
    ) is True
    assert expired() == "replacement-bearer"


def test_bind_requires_a_trusted_controller_and_keeps_responses_secret_free():
    agent = _Agent()
    server._sessions["native-sid"] = _session(agent)

    denied = server.dispatch(
        {"id": 1, "method": "session.credential.bind", "params": _bind_params()},
        _Transport(False),
    )
    assert denied["error"]["message"] == "credential unavailable"

    response = server.dispatch(
        {"id": 2, "method": "session.credential.bind", "params": _bind_params()},
        _Transport(True),
    )
    assert response["result"] == {"status": "ready", "credential_slot": "GATE_B_API_KEY"}
    assert "test-bearer-never-log" not in repr(response)
    assert len(agent.calls) == 1
    assert agent.calls[0]["api_key"] is server._sessions["native-sid"]["credential_holder"]


def test_same_revision_refreshes_one_holder_without_switching_and_route_changes_fail_closed():
    agent = _Agent()
    server._sessions["native-sid"] = _session(agent)
    trusted = _Transport(True)
    first = server.dispatch(
        {"id": 1, "method": "session.credential.bind", "params": _bind_params()}, trusted
    )
    holder = server._sessions["native-sid"]["credential_holder"]

    refreshed = server.dispatch(
        {
            "id": 2,
            "method": "session.credential.bind",
            "params": _bind_params(bearer="second-bearer", expires_at=_expiry()),
        },
        trusted,
    )
    changed = server.dispatch(
        {
            "id": 3,
            "method": "session.credential.bind",
            "params": _bind_params(provider_route_revision_id="different-opaque-revision"),
        },
        trusted,
    )

    assert first["result"]["status"] == refreshed["result"]["status"] == "ready"
    assert server._sessions["native-sid"]["credential_holder"] is holder
    assert holder() == "second-bearer"
    assert len(agent.calls) == 1
    assert changed["error"]["message"] == "credential unavailable"


def test_two_live_sessions_keep_independent_credential_holders():
    server._sessions["one"] = _session(_Agent(), key="one-key")
    server._sessions["two"] = _session(_Agent(), key="two-key")
    trusted = _Transport(True)

    server.dispatch(
        {"id": 1, "method": "session.credential.bind", "params": _bind_params(session_id="one", bearer="one-bearer")},
        trusted,
    )
    server.dispatch(
        {"id": 2, "method": "session.credential.bind", "params": _bind_params(session_id="two", bearer="two-bearer")},
        trusted,
    )

    assert server._sessions["one"]["credential_holder"] is not server._sessions["two"]["credential_holder"]
    assert server._sessions["one"]["credential_holder"]() == "one-bearer"
    assert server._sessions["two"]["credential_holder"]() == "two-bearer"


def test_holder_present_before_agent_activation_replaces_static_key_once():
    holder = server._SessionCredential("deferred-bearer", datetime.now(timezone.utc) + timedelta(minutes=1))
    session = {"credential_holder": holder}
    agent = _Agent()
    agent.api_key = "static-key"

    server._activate_session_credential(session, agent)
    server._activate_session_credential(session, agent)

    assert len(agent.calls) == 1
    assert agent.calls[0]["api_key"] is holder
    assert agent.calls[0]["new_model"] == "stock-model"
    assert agent.calls[0]["new_provider"] == "stock-provider"


def test_deferred_build_and_bind_publish_only_a_holder_activated_agent(monkeypatch):
    agent = _Agent()
    session = {
        "agent": None,
        "agent_ready": threading.Event(),
        "session_key": "deferred-key",
        "source": "tui",
    }
    server._sessions["native-sid"] = session
    monkeypatch.setattr(server, "_make_agent", lambda *args, **kwargs: agent)
    monkeypatch.setattr(server, "_set_session_context", lambda key: None)
    monkeypatch.setattr(server, "_clear_session_context", lambda token: None)
    monkeypatch.setattr(server, "_config_model_target", lambda: None)
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    monkeypatch.setattr(server, "_load_memory_notifications", lambda: False)
    monkeypatch.setattr(server, "_start_notification_poller", lambda sid, record: None)
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *args: None)
    monkeypatch.setattr(server, "_session_info", lambda agent, record: {})
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_probe_config_health", lambda cfg: None)
    monkeypatch.setattr(server, "_emit", lambda *args: None)
    monkeypatch.setattr(server, "_schedule_mcp_late_refresh", lambda sid, agent: None)
    activation_attempted = threading.Event()
    release_publication = threading.Event()
    real_activate = server._activate_session_credential

    def pause_after_activation(record, built_agent=None):
        real_activate(record, built_agent)
        if built_agent is agent:
            activation_attempted.set()
            assert release_publication.wait(timeout=2)

    monkeypatch.setattr(server, "_activate_session_credential", pause_after_activation)
    server._start_agent_build("native-sid", session)
    assert activation_attempted.wait(timeout=2)
    responses = []

    def bind():
        responses.append(
            server.dispatch(
                {"id": 1, "method": "session.credential.bind", "params": _bind_params()},
                _Transport(True),
            )
        )

    binder = threading.Thread(target=bind)
    binder.start()
    try:
        assert binder.join(timeout=0.1) is None
        assert binder.is_alive()
    finally:
        release_publication.set()
        binder.join(timeout=2)
        assert session["agent_ready"].wait(timeout=2)

    holder = session["credential_holder"]
    assert responses[0]["result"]["status"] == "ready"
    assert session["agent"] is agent
    assert agent.calls[-1]["api_key"] is holder


@pytest.mark.parametrize(
    "params",
    [
        _bind_params(credential_slot="other"),
        _bind_params(bearer=""),
        _bind_params(expires_at="2030-01-01 00:00:00Z"),
        _bind_params(expires_at="2000-01-01T00:00:00Z"),
        {"session_id": "native-sid"},
    ],
)
def test_bind_validation_is_exact_and_redacted(params):
    server._sessions["native-sid"] = _session()

    response = server.dispatch(
        {"id": 1, "method": "session.credential.bind", "params": params}, _Transport(True)
    )

    assert response["error"]["message"] == "credential unavailable"
    assert "credential_holder" not in server._sessions["native-sid"]


def test_missing_live_session_fails_closed_after_process_loss():
    response = server.dispatch(
        {"id": 1, "method": "session.credential.bind", "params": _bind_params()}, _Transport(True)
    )

    assert response["error"]["message"] == "credential unavailable"


def test_bound_sessions_reject_untrusted_native_and_stored_key_dispatches_then_revoke_on_close():
    holder = server._SessionCredential("close-bearer", datetime.now(timezone.utc) + timedelta(minutes=1))
    agent = _Agent()
    agent.session_id = "different-native-agent-key"
    server._sessions["native-sid"] = _session(agent, key="stored-key")
    server._sessions["native-sid"]["credential_holder"] = holder
    original_prompt = server._methods["prompt.submit"]
    original_resume = server._methods["session.resume"]
    calls = []
    server._methods["prompt.submit"] = lambda rid, params: calls.append("prompt") or server._ok(rid, {})
    server._methods["session.resume"] = lambda rid, params: calls.append("resume") or server._ok(rid, {})
    try:
        for method, sid in (("prompt.submit", "native-sid"), ("session.resume", "stored-key"), ("session.close", "native-sid")):
            response = server.dispatch(
                {"id": 1, "method": method, "params": {"session_id": sid}}, _Transport(False)
            )
            assert response["error"]["message"] == "session unavailable"
        assert calls == []

        closed = server.dispatch(
            {"id": 2, "method": "session.close", "params": {"session_id": "native-sid"}},
            _Transport(True),
        )
        assert closed["result"] == {"closed": True}
        with pytest.raises(RuntimeError, match="credential unavailable"):
            holder()
    finally:
        server._methods["prompt.submit"] = original_prompt
        server._methods["session.resume"] = original_resume
