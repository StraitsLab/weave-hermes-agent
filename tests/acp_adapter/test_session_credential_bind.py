from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from acp.agent.router import build_agent_router
from acp_adapter.server import HermesACPAgent
from acp_adapter.session import SessionManager


class _Agent:
    model = "stock-model"
    provider = "stock-provider"
    base_url = "https://stock.example/v1"
    api_mode = "chat_completions"

    def __init__(self) -> None:
        self.calls = []

    def switch_model(self, **kwargs) -> None:
        self.calls.append(kwargs)


class _PrivateTransport:
    pass


def _expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")


def _bind_params(session_id: str, **overrides) -> dict:
    params = {
        "session_id": session_id,
        "credential_slot": "GATE_B_API_KEY",
        "bearer": "test-bearer-never-log",
        "expires_at": _expiry(),
        "provider_route_revision_id": "opaque-route-revision",
    }
    params.update(overrides)
    return params


@pytest.mark.asyncio
async def test_private_extension_binds_only_its_live_acp_session_and_redacts_response():
    manager = SessionManager(agent_factory=_Agent)
    agent = HermesACPAgent(session_manager=manager)
    agent.on_connect(_PrivateTransport())
    await agent.initialize(protocol_version=1)
    state = manager.create_session(cwd="/tmp")

    response = await build_agent_router(agent)(
        "_session/credential/bind", _bind_params(state.session_id), False
    )

    assert response == {"status": "ready", "credential_slot": "GATE_B_API_KEY"}
    assert "test-bearer-never-log" not in repr(response)
    assert len(state.agent.calls) == 1
    assert state.agent.calls[0]["api_key"] is state.credential_holder
    assert state.credential_holder() == "test-bearer-never-log"


@pytest.mark.asyncio
async def test_same_revision_refreshes_the_live_holder_without_a_second_switch():
    manager = SessionManager(agent_factory=_Agent)
    agent = HermesACPAgent(session_manager=manager)
    agent.on_connect(_PrivateTransport())
    await agent.initialize(protocol_version=1)
    state = manager.create_session(cwd="/tmp")

    await agent.ext_method("session/credential/bind", _bind_params(state.session_id))
    holder = state.credential_holder
    refreshed = await agent.ext_method(
        "session/credential/bind",
        _bind_params(state.session_id, bearer="replacement-bearer"),
    )
    changed = await agent.ext_method(
        "session/credential/bind",
        _bind_params(state.session_id, provider_route_revision_id="different-revision"),
    )

    assert refreshed == {"status": "ready", "credential_slot": "GATE_B_API_KEY"}
    assert state.credential_holder is holder
    assert holder() == "replacement-bearer"
    assert len(state.agent.calls) == 1
    assert changed == {"error": "credential unavailable"}


@pytest.mark.asyncio
async def test_bind_rejects_unknown_malformed_unauthenticated_and_foreign_transports():
    manager = SessionManager(agent_factory=_Agent)
    agent = HermesACPAgent(session_manager=manager)
    state = manager.create_session(cwd="/tmp")

    unauthenticated = await agent.ext_method("session/credential/bind", _bind_params(state.session_id))
    agent.on_connect(_PrivateTransport())
    await agent.initialize(protocol_version=1)
    malformed = await agent.ext_method(
        "session/credential/bind",
        _bind_params(state.session_id, expires_at="not-an-rfc3339-timestamp"),
    )
    unknown = await agent.ext_method("session/credential/bind", _bind_params("missing"))
    await agent.ext_method("session/credential/bind", _bind_params(state.session_id))
    holder = state.credential_holder
    agent.on_connect(_PrivateTransport())
    foreign = await agent.ext_method("session/credential/bind", _bind_params(state.session_id))

    assert unauthenticated == malformed == unknown == foreign == {"error": "credential unavailable"}
    with pytest.raises(RuntimeError, match="credential unavailable"):
        holder()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides, whitespace_session_id",
        [
            ({"unexpected": "field"}, False),
            ({}, True),
        ({"bearer": " \t "}, False),
        ({"provider_route_revision_id": " \t "}, False),
    ],
)
async def test_bind_requires_exact_fields_and_stripped_values(overrides, whitespace_session_id):
    manager = SessionManager(agent_factory=_Agent)
    agent = HermesACPAgent(session_manager=manager)
    agent.on_connect(_PrivateTransport())
    await agent.initialize(protocol_version=1)
    state = manager.create_session(cwd="/tmp")
    session_id = state.session_id
    if whitespace_session_id:
        with manager._lock:
            manager._sessions.pop(session_id)
            state.session_id = " \t "
            manager._sessions[state.session_id] = state
        session_id = state.session_id

    response = await agent.ext_method(
        "session/credential/bind", _bind_params(session_id, **overrides)
    )

    assert response == {"error": "credential unavailable"}
    assert state.credential_holder is None


@pytest.mark.asyncio
async def test_close_and_process_cleanup_revoke_the_bound_credential():
    manager = SessionManager(agent_factory=_Agent)
    agent = HermesACPAgent(session_manager=manager)
    agent.on_connect(_PrivateTransport())
    await agent.initialize(protocol_version=1)
    state = manager.create_session(cwd="/tmp")
    await agent.ext_method("session/credential/bind", _bind_params(state.session_id))
    holder = state.credential_holder

    await agent.close_session(session_id=state.session_id)
    with pytest.raises(RuntimeError, match="credential unavailable"):
        holder()

    second = manager.create_session(cwd="/tmp")
    await agent.ext_method("session/credential/bind", _bind_params(second.session_id))
    second_holder = second.credential_holder
    manager.cleanup()
    with pytest.raises(RuntimeError, match="credential unavailable"):
        second_holder()
