"""Behavior contracts for native gateway-process session admission."""

import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.run import GatewayRunner
from hermes_state import SessionDB


SESSION_ID = "native-submit-session"


def _request(request_id="request-1", message="hello"):
    return {
        "kind": "hermes.session.submit",
        "external_request_id": request_id,
        "message": message,
        "busy_mode": "queue",
    }


@pytest.fixture
def adapter(tmp_path):
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "sk-native-submit-test"})
    )
    db = SessionDB(tmp_path / "state.db")
    db.create_session(SESSION_ID, "api_server")
    adapter._session_db = db
    try:
        yield adapter
    finally:
        db.close()


async def _client(adapter):
    app = web.Application()
    app.router.add_post(
        "/api/sessions/{session_id}/submit", adapter._handle_session_submit
    )
    app.router.add_get(
        "/api/sessions/{session_id}/submit/{native_request_ref}/clarify",
        adapter._handle_native_submit_clarify_events,
    )
    app.router.add_post(
        "/api/sessions/{session_id}/submit/{native_request_ref}/clarify/{clarify_id}",
        adapter._handle_native_submit_clarify_response,
    )
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_submit_returns_native_receipt_and_reuses_identical_request(adapter, monkeypatch):
    calls = []

    async def admit(session_id, message, native_request_ref):
        calls.append((session_id, message, native_request_ref))
        return "streaming"

    monkeypatch.setattr(adapter, "_admit_native_session_submit", admit)
    client = await _client(adapter)
    headers = {"Authorization": "Bearer sk-native-submit-test"}
    try:
        first = await client.post(f"/api/sessions/{SESSION_ID}/submit", headers=headers, json=_request())
        second = await client.post(f"/api/sessions/{SESSION_ID}/submit", headers=headers, json=_request())
        first_body, second_body = await first.json(), await second.json()
    finally:
        await client.close()

    assert first.status == 202
    assert second.status == 202
    assert first_body == second_body
    assert first_body["object"] == "hermes.session.admission"
    assert first_body["admission"] == "streaming"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_submit_rejects_changed_retry_and_non_queue_busy_modes(adapter, monkeypatch):
    async def admit(session_id, message, native_request_ref):
        return "queued"

    monkeypatch.setattr(adapter, "_admit_native_session_submit", admit)
    client = await _client(adapter)
    headers = {"Authorization": "Bearer sk-native-submit-test"}
    try:
        accepted = await client.post(f"/api/sessions/{SESSION_ID}/submit", headers=headers, json=_request())
        changed = await client.post(f"/api/sessions/{SESSION_ID}/submit", headers=headers, json=_request(message="changed"))
        changed_body = await changed.json()
        invalid = await client.post(
            f"/api/sessions/{SESSION_ID}/submit", headers=headers,
            json={**_request("request-2"), "busy_mode": "steer"},
        )
        invalid_body = await invalid.json()
    finally:
        await client.close()

    assert accepted.status == 202
    assert changed.status == 409
    assert changed_body["error"]["code"] == "native_submit_idempotency_conflict"
    assert invalid.status == 400
    assert invalid_body["error"]["code"] == "invalid_native_submit_schema"


@pytest.mark.asyncio
async def test_pending_retry_reenters_native_admission_instead_of_faking_streaming(adapter, monkeypatch):
    request = _request("crash-window")
    adapter._session_db.register_native_session_submit(
        SESSION_ID, external_request_id="crash-window",
        message_sha256=hashlib.sha256(b"hello").hexdigest(),
        native_request_ref="same-native-ref",
    )
    admitted = []

    async def admit(session_id, message, native_request_ref):
        admitted.append((session_id, message, native_request_ref))
        return "streaming"

    monkeypatch.setattr(adapter, "_admit_native_session_submit", admit)
    client = await _client(adapter)
    try:
        response = await client.post(
            f"/api/sessions/{SESSION_ID}/submit",
            headers={"Authorization": "Bearer sk-native-submit-test"}, json=request,
        )
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 202
    assert admitted == [(SESSION_ID, "hello", "same-native-ref")]
    assert body["native_request_ref"] == "same-native-ref"


@pytest.mark.asyncio
async def test_matching_clarify_response_resolves_only_its_native_waiter(adapter, monkeypatch):
    ref = "native-ref-1"
    adapter._native_submit_active_refs["native-key"] = ref
    adapter._native_submit_ref_sessions[ref] = ("default", SESSION_ID)
    await adapter.send_clarify(
        chat_id=SESSION_ID,
        question="Pick one",
        choices=["A", "B"],
        clarify_id="clarify-1",
        session_key="native-key",
    )
    resolved = []
    monkeypatch.setattr(
        "tools.clarify_gateway.resolve_gateway_clarify",
        lambda clarify_id, response: resolved.append((clarify_id, response)) or True,
    )
    client = await _client(adapter)
    headers = {"Authorization": "Bearer sk-native-submit-test"}
    try:
        events = await client.get(
            f"/api/sessions/{SESSION_ID}/submit/{ref}/clarify", headers=headers
        )
        repeated_events = await client.get(
            f"/api/sessions/{SESSION_ID}/submit/{ref}/clarify", headers=headers
        )
        response = await client.post(
            f"/api/sessions/{SESSION_ID}/submit/{ref}/clarify/clarify-1",
            headers=headers, json={"response": "B"},
        )
        cleared_events = await client.get(
            f"/api/sessions/{SESSION_ID}/submit/{ref}/clarify", headers=headers
        )
        duplicate = await client.post(
            f"/api/sessions/{SESSION_ID}/submit/{ref}/clarify/clarify-1",
            headers=headers, json={"response": "B"},
        )
        events_body, repeated_events_body, response_body, cleared_events_body, duplicate_body = (
            await events.json(), await repeated_events.json(), await response.json(),
            await cleared_events.json(), await duplicate.json()
        )
    finally:
        await client.close()

    assert events.status == 200
    assert events_body["data"] == [{
        "type": "clarify.request", "native_request_ref": ref,
        "clarify_id": "clarify-1", "question": "Pick one",
        "choices": ["A", "B"], "multi_select": False,
    }]
    assert repeated_events_body == events_body
    assert cleared_events_body["data"] == []
    assert response.status == 200
    assert response_body["resolved"] is True
    assert resolved == [("clarify-1", "B")]
    assert duplicate.status == 409
    assert duplicate_body["error"]["code"] == "native_clarify_terminal"


@pytest.mark.asyncio
async def test_terminal_native_turn_closes_its_unanswered_clarify(adapter):
    adapter._session_db.register_native_session_submit(
        SESSION_ID, external_request_id="terminal-request",
        message_sha256="0" * 64, native_request_ref="native-ref-2",
    )
    event = type("Event", (), {"metadata": {"native_request_ref": "native-ref-2"}})()
    await adapter._on_native_submit_started(event, "native-key")
    await adapter.send_clarify(
        chat_id=SESSION_ID, question="Need answer", choices=None,
        clarify_id="clarify-2", session_key="native-key",
    )
    await adapter._on_native_submit_finished(event, "native-key")

    assert adapter._native_submit_active_refs == {}
    assert adapter._native_submit_clarifies[("native-ref-2", "clarify-2")] == "terminal"


@pytest.mark.asyncio
async def test_native_submit_uses_adapter_writer_or_existing_runner_fifo(adapter, monkeypatch):
    entry = SimpleNamespace(session_key="native-key", session_id=SESSION_ID)
    store = SimpleNamespace(
        bind_existing_session=AsyncMock(return_value=entry),
    )
    queued = []
    runner = SimpleNamespace(
        _running=True,
        async_session_store=store,
        _is_session_running=lambda key: False,
        _enqueue_fifo=lambda key, event, adapter: queued.append((key, event)),
    )
    adapter.gateway_runner = runner
    adapter._session_db.register_native_session_submit(
        SESSION_ID, external_request_id="writer-request",
        message_sha256="0" * 64, native_request_ref="native-1",
    )

    async def start_native_event(event):
        await adapter._on_native_submit_started(event, "native-key")

    adapter.handle_message = AsyncMock(side_effect=start_native_event)

    assert await adapter._admit_native_session_submit(SESSION_ID, "first", "native-1") == "streaming"
    event = adapter.handle_message.await_args.args[0]
    assert adapter._native_submit_ref_sessions["native-1"] == ("default", SESSION_ID)
    assert event.internal is False
    assert event.metadata["native_submit_authenticated"] is True
    assert event.metadata["gateway_session_strict"] is True
    assert event.metadata["native_request_ref"] == "native-1"

    adapter._active_sessions["native-key"] = asyncio.Event()
    assert await adapter._admit_native_session_submit(SESSION_ID, "second", "native-2") == "queued"
    assert [(key, event.text) for key, event in queued] == [("native-key", "second")]
    assert adapter.handle_message.await_count == 1


@pytest.mark.asyncio
async def test_native_submit_fails_closed_while_gateway_is_draining(adapter):
    adapter.gateway_runner = SimpleNamespace(_running=True, _draining=True)

    with pytest.raises(RuntimeError, match="unavailable"):
        await adapter._admit_native_session_submit(SESSION_ID, "hello", "native-drain")


@pytest.mark.asyncio
async def test_real_runner_keeps_one_writer_and_uses_fifo_for_native_submit(tmp_path, monkeypatch):
    """Exercise the actual SessionStore, GatewayRunner, and adapter guard."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = GatewayRunner(GatewayConfig())
    runner._running = True
    db = runner._session_db._db
    db.create_session("real-native-session", "api_server")
    for request_id, ref, message in (("real-request-1", "real-1", "first"), ("real-request-2", "real-2", "second")):
        db.register_native_session_submit(
            "real-native-session", external_request_id=request_id,
            message_sha256=hashlib.sha256(message.encode()).hexdigest(),
            native_request_ref=ref,
        )
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "sk-real-native-test"}))
    adapter._session_db = db
    adapter.gateway_runner = runner
    adapter.set_session_store(runner.session_store)
    adapter.set_message_handler(runner._handle_message)
    runner.adapters = {Platform.API_SERVER: adapter}
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def fake_turn(event, source, session_key, generation):
        calls.append(event.message_id)
        started.set()
        await release.wait()
        return ""

    monkeypatch.setattr(runner, "_handle_message_with_agent", fake_turn)
    try:
        first = await adapter._admit_native_session_submit("real-native-session", "first", "real-1")
        await started.wait()
        second = await adapter._admit_native_session_submit("real-native-session", "second", "real-2")
        assert first == "streaming"
        assert second == "queued"
        assert calls == ["real-1"]
        release.set()
        for _ in range(20):
            if calls == ["real-1", "real-2"]:
                break
            await asyncio.sleep(0.01)
        assert calls == ["real-1", "real-2"]
    finally:
        release.set()
        await adapter.cancel_background_tasks()
        db.close()
