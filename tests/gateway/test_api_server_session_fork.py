"""Authenticated native safe-fork REST behavior."""

import asyncio
from datetime import datetime, timedelta, timezone
import threading

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.run import GatewayRunner
from gateway.session import SessionSource


PREDECESSOR = "fork-predecessor"
ROTATION = "11111111-1111-4111-8111-111111111111"
BOUNDARY = "22222222-2222-4222-8222-222222222222"
SUCCESSOR = f"weave-{ROTATION}"
AUTH = {"Authorization": "Bearer sk-safe-fork-test"}


def _request(**changes):
    body = {"external_rotation_id": ROTATION, "boundary_turn_id": BOUNDARY}
    body.update(changes)
    return body


def _source(session_id=PREDECESSOR):
    return SessionSource(
        platform=Platform.API_SERVER, chat_id=session_id, chat_type="dm",
        user_id="api_server", user_name="API server",
    )


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = GatewayRunner(GatewayConfig())
    runner._running = True
    db = runner._session_db._db
    db.create_session(PREDECESSOR, "api_server")
    db.append_message(PREDECESSOR, "user", "secret-free transcript")
    expires = datetime.now(timezone.utc) + timedelta(minutes=5)
    assert runner.bind_session_credential(_source(), PREDECESSOR, "bearer-must-not-escape", expires, "route-a")
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "sk-safe-fork-test"}))
    adapter._session_db = db
    adapter.gateway_runner = runner
    try:
        yield adapter, runner, db
    finally:
        db.close()


async def _client(adapter):
    app = web.Application()
    for method, path, handler in adapter._http_route_table():
        app.router.add_route(method, path, handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_fork_requires_auth_and_exact_closed_request(setup):
    adapter, _, db = setup
    client = await _client(adapter)
    try:
        unauthenticated = await client.post(f"/api/sessions/{PREDECESSOR}/fork", json=_request())
        unknown = await client.post(f"/api/sessions/{PREDECESSOR}/fork", headers=AUTH,
                                    json=_request(extra="rejected"))
        malformed = await client.post(f"/api/sessions/{PREDECESSOR}/fork", headers=AUTH, json=_request(external_rotation_id="not-a-uuid"))
    finally:
        await client.close()

    assert unauthenticated.status == 401
    assert unknown.status == malformed.status == 400
    assert db.get_session(SUCCESSOR) is None


@pytest.mark.asyncio
async def test_fork_repoints_evicts_revokes_and_returns_closed_receipt(setup):
    adapter, runner, db = setup
    key = runner.session_store._generate_session_key(_source())
    runner._agent_cache[key] = object()
    client = await _client(adapter)
    try:
        response = await client.post(f"/api/sessions/{PREDECESSOR}/fork", headers=AUTH, json=_request())
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 201
    assert body == {
        "outcome": "forked",
        "external_rotation_id": ROTATION,
        "boundary_turn_id": BOUNDARY,
        "predecessor_session_id": PREDECESSOR,
        "successor_session_id": SUCCESSOR,
        "native_input_queue_empty": True,
        "no_active_turn": True,
        "no_compression_or_session_mutation_lock": True,
    }
    assert "bearer-must-not-escape" not in repr(body)
    successor_key = runner.session_store._generate_session_key(_source(SUCCESSOR))
    assert runner.session_store._entries[successor_key].session_id == SUCCESSOR
    assert key not in runner.session_store._entries
    assert key not in runner._agent_cache
    assert not runner.session_credential_available(_source(), PREDECESSOR)
    assert db.get_session(SUCCESSOR)["ended_at"] is None


@pytest.mark.asyncio
async def test_busy_memory_queue_and_active_turn_are_retryable(setup):
    adapter, runner, db = setup
    key = runner.session_store._generate_session_key(_source())
    adapter._pending_messages[key] = object()
    client = await _client(adapter)
    try:
        queued = await client.post(f"/api/sessions/{PREDECESSOR}/fork", headers=AUTH, json=_request())
        adapter._pending_messages.clear()
        runner._session_state(key).turn.agent = object()
        active = await client.post(f"/api/sessions/{PREDECESSOR}/fork", headers=AUTH, json=_request())
        queued_body, active_body = await queued.json(), await active.json()
    finally:
        await client.close()

    assert queued.status == active.status == 409
    assert queued_body["error"]["code"] == "session_boundary_unavailable"
    assert active_body["error"]["code"] == "session_boundary_unavailable"
    assert db.get_session(SUCCESSOR) is None


@pytest.mark.asyncio
async def test_postcommit_repoint_failure_repairs_on_identical_replay(setup, monkeypatch):
    adapter, runner, db = setup
    store = runner.session_store
    real_switch = store.switch_session
    calls = []

    def fail_once(session_key, successor):
        calls.append((session_key, successor))
        return None if len(calls) == 1 else real_switch(session_key, successor)

    monkeypatch.setattr(store, "switch_session", fail_once)
    client = await _client(adapter)
    try:
        failed = await client.post(f"/api/sessions/{PREDECESSOR}/fork", headers=AUTH, json=_request())
        replay = await client.post(f"/api/sessions/{PREDECESSOR}/fork", headers=AUTH, json=_request())
        failed_body, replay_body = await failed.json(), await replay.json()
    finally:
        await client.close()

    assert failed.status == 409
    assert failed_body["error"]["code"] == "session_boundary_unavailable"
    assert replay.status == 200
    assert replay_body["outcome"] == "identical_retry"
    key = store._generate_session_key(_source(SUCCESSOR))
    assert store._entries[key].session_id == SUCCESSOR
    assert db.get_session(PREDECESSOR)["end_reason"] == "branched"


@pytest.mark.asyncio
async def test_successor_can_bind_and_submit_while_predecessor_is_denied(setup, monkeypatch):
    adapter, _, _ = setup
    admitted = []

    async def admit(*args):
        admitted.append(args)
        return "streaming"

    monkeypatch.setattr(adapter, "_admit_native_session_submit", admit)
    credential = {
        "credential_slot": "GATE_B_API_KEY",
        "bearer": "successor-only-bearer",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "provider_route_revision_id": "route-b",
    }
    submit = {
        "kind": "hermes.session.submit", "external_request_id": "next-turn",
        "message": "continue", "busy_mode": "queue",
    }
    client = await _client(adapter)
    try:
        await client.post(f"/api/sessions/{PREDECESSOR}/fork", headers=AUTH, json=_request())
        old = await client.post(f"/api/sessions/{PREDECESSOR}/credential/bind", headers=AUTH, json=credential)
        bound = await client.post(f"/api/sessions/{SUCCESSOR}/credential/bind", headers=AUTH, json=credential)
        accepted = await client.post(f"/api/sessions/{SUCCESSOR}/submit", headers=AUTH, json=submit)
    finally:
        await client.close()

    assert old.status == 409
    assert bound.status == 200
    assert accepted.status == 202
    assert admitted and admitted[0][:2] == (SUCCESSOR, "continue")


@pytest.mark.asyncio
@pytest.mark.parametrize("method,suffix,payload", [
    ("POST", "credential/bind", {
        "credential_slot": "GATE_B_API_KEY", "bearer": "new-bearer", "expires_at": "2099-01-01T00:00:00Z", "provider_route_revision_id": "route-a",
    }),
    ("POST", "submit", {"kind": "hermes.session.submit", "external_request_id": "waiting-turn", "message": "wait", "busy_mode": "queue"}),
    ("PATCH", "", {"end_reason": "closed"}),
    ("DELETE", "", None),
])
async def test_successor_lifecycle_waits_for_fork_repoint(setup, monkeypatch, method, suffix, payload):
    adapter, runner, db = setup
    store = runner.session_store
    real_switch = store.switch_session
    switch_entered = threading.Event()
    release_switch = threading.Event()

    def paused_switch(session_key, successor):
        switch_entered.set()
        assert release_switch.wait(2)
        return real_switch(session_key, successor)

    monkeypatch.setattr(store, "switch_session", paused_switch)
    path = f"/api/sessions/{SUCCESSOR}" + (f"/{suffix}" if suffix else "")
    client = await _client(adapter)
    try:
        fork = asyncio.create_task(client.post(f"/api/sessions/{PREDECESSOR}/fork", headers=AUTH, json=_request()))
        assert await asyncio.to_thread(switch_entered.wait, 2)
        operation = asyncio.create_task(client.request(method, path, headers=AUTH, json=payload))
        successor_lock = ("default", SUCCESSOR)
        async def operation_observed():
            while not operation.done() and adapter._native_submit_lock_refs.get(successor_lock) != 2:
                await asyncio.sleep(0)
        await asyncio.wait_for(operation_observed(), 2)
        assert not operation.done()
        assert adapter._native_submit_lock_refs[successor_lock] == 2
        release_switch.set()
        fork_response, operation_response = await asyncio.gather(fork, operation)
        assert fork_response.status == 201
        assert operation_response.status in {200, 409}
        if method != "DELETE":
            keys = [key for key, entry in store._entries.items() if entry.session_id == SUCCESSOR]
            assert keys == [store._generate_session_key(_source(SUCCESSOR))]
            assert db.get_session(SUCCESSOR) is not None
    finally:
        release_switch.set()
        await client.close()
