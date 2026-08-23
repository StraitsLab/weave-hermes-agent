"""Behavior contracts for native admitted-turn SSE events."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB


SESSION_ID = "native-submit-events-session"
REQUEST_REF = "native-request-ref"


@pytest.fixture
def adapter(tmp_path):
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "sk-native-events-test"})
    )
    db = SessionDB(tmp_path / "state.db")
    db.create_session(SESSION_ID, "api_server")
    adapter._session_db = db
    adapter._native_submit_ref_sessions[REQUEST_REF] = ("default", SESSION_ID)
    try:
        yield adapter
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
async def test_native_submit_events_opens_an_authenticated_live_sse_feed(adapter):
    client = await _client(adapter)
    try:
        response = await client.get(
            f"/api/sessions/{SESSION_ID}/submit/{REQUEST_REF}/events",
            headers={"Authorization": "Bearer sk-native-events-test"},
        )
        assert response.status == 200
        assert response.headers["Content-Type"].startswith("text/event-stream")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_native_turn_callbacks_emit_one_ordered_bounded_projection(adapter):
    adapter._session_db.register_native_session_submit(
        SESSION_ID,
        external_request_id="events-request",
        message_sha256="0" * 64,
        native_request_ref=REQUEST_REF,
    )
    queue = asyncio.Queue(maxsize=32)
    adapter._native_submit_subscribers[REQUEST_REF] = {
        id(queue): (queue, asyncio.get_running_loop()),
    }
    event = SimpleNamespace(
        metadata={"native_request_ref": REQUEST_REF},
        source=SimpleNamespace(chat_id=SESSION_ID, profile=None),
    )

    await adapter._on_native_submit_started(event, "native-session-key")
    await asyncio.sleep(0)

    assert not queue.empty()
    adapter._native_submit_delta("native-session-key", "hello" * 2_000)
    adapter._native_submit_tool_started("native-session-key", "call-1", "search")
    adapter._native_submit_tool_completed("native-session-key", "call-1", "search", "ok")
    adapter._native_submit_final("native-session-key", "done")
    await adapter._on_native_submit_finished(event, "native-session-key")
    await asyncio.sleep(0)

    events = []
    while not queue.empty():
        item = queue.get_nowait()
        if item is not None:
            events.append(item)
    assert [item["type"] for item in events] == [
        "turn.started", "assistant.delta", "tool.started", "tool.completed",
        "assistant.final", "turn.completed",
    ]
    assert [item["sequence"] for item in events] == list(range(1, 7))
    assert len(events[1]["delta"]) == 4_096
    assert events[2] == {
        "native_request_ref": REQUEST_REF,
        "sequence": 3,
        "type": "tool.started",
        "tool_call_id": "call-1",
        "tool_name": "search",
    }


@pytest.mark.asyncio
async def test_native_turn_failure_closes_the_live_feed_as_failed(adapter):
    adapter._session_db.register_native_session_submit(
        SESSION_ID,
        external_request_id="failed-events-request",
        message_sha256="0" * 64,
        native_request_ref=REQUEST_REF,
    )
    queue = asyncio.Queue(maxsize=32)
    adapter._native_submit_subscribers[REQUEST_REF] = {
        id(queue): (queue, asyncio.get_running_loop()),
    }
    event = SimpleNamespace(
        metadata={"native_request_ref": REQUEST_REF, "native_submit_failed": True},
        source=SimpleNamespace(chat_id=SESSION_ID, profile=None),
    )

    await adapter._on_native_submit_started(event, "failed-session-key")
    await adapter._on_native_submit_finished(event, "failed-session-key")

    assert queue.get_nowait()["type"] == "turn.started"
    assert queue.get_nowait()["type"] == "turn.failed"
    assert queue.get_nowait() is None
    assert adapter._native_submit_events == {}
    assert adapter._native_submit_subscribers == {}


@pytest.mark.asyncio
async def test_events_reject_cross_session_and_terminal_native_refs(adapter):
    client = await _client(adapter)
    headers = {"Authorization": "Bearer sk-native-events-test"}
    try:
        unauthorized = await client.get(
            f"/api/sessions/{SESSION_ID}/submit/{REQUEST_REF}/events"
        )
        wrong_session = await client.get(
            f"/api/sessions/not-{SESSION_ID}/submit/{REQUEST_REF}/events",
            headers=headers,
        )
        adapter._native_submit_terminals.add(REQUEST_REF)
        terminal = await client.get(
            f"/api/sessions/{SESSION_ID}/submit/{REQUEST_REF}/events",
            headers=headers,
        )
        terminal_body = await terminal.json()
    finally:
        await client.close()

    assert unauthorized.status == 401
    assert wrong_session.status == 404
    assert terminal.status == 409
    assert terminal_body["error"]["code"] == "native_admission_terminal"


@pytest.mark.asyncio
async def test_reconnect_seeds_only_the_current_correlated_clarification(adapter):
    adapter._native_submit_active_refs["native-session-key"] = REQUEST_REF
    await adapter.send_clarify(
        chat_id=SESSION_ID,
        question="Pick one",
        choices=["A", "B"],
        clarify_id="clarify-1",
        session_key="native-session-key",
    )
    client = await _client(adapter)
    try:
        response = await client.get(
            f"/api/sessions/{SESSION_ID}/submit/{REQUEST_REF}/events",
            headers={"Authorization": "Bearer sk-native-events-test"},
        )
        assert await asyncio.wait_for(response.content.readline(), timeout=1) == b"event: clarify.request\n"
        payload = json.loads((await response.content.readline()).removeprefix(b"data: ").decode())
    finally:
        await client.close()

    assert payload["native_request_ref"] == REQUEST_REF
    assert payload["clarify_id"] == "clarify-1"
    assert payload["question"] == "Pick one"
    assert payload["choices"] == ["A", "B"]
    assert payload["multi_select"] is False


@pytest.mark.asyncio
async def test_slow_subscriber_is_released_without_retaining_event_bodies(adapter):
    queue = asyncio.Queue(maxsize=1)
    adapter._native_submit_subscribers[REQUEST_REF] = {
        id(queue): (queue, asyncio.get_running_loop()),
    }

    adapter._native_submit_event(REQUEST_REF, "turn.started")
    adapter._native_submit_event(REQUEST_REF, "assistant.delta", delta="later")

    assert queue.get_nowait() is None
    assert adapter._native_submit_subscribers[REQUEST_REF] == {}
    assert adapter._native_submit_events == {}


@pytest.mark.asyncio
async def test_client_disconnect_releases_its_live_subscriber(adapter):
    client = await _client(adapter)
    try:
        response = await client.get(
            f"/api/sessions/{SESSION_ID}/submit/{REQUEST_REF}/events",
            headers={"Authorization": "Bearer sk-native-events-test"},
        )
        assert adapter._native_submit_subscribers[REQUEST_REF]
        response.close()
        adapter._native_submit_event(REQUEST_REF, "turn.started")
        for _ in range(20):
            if not adapter._native_submit_subscribers.get(REQUEST_REF):
                break
            await asyncio.sleep(0.01)
    finally:
        await client.close()

    assert adapter._native_submit_subscribers.get(REQUEST_REF, {}) == {}
