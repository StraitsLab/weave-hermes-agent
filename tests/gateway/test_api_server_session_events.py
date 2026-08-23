"""Behavior contracts for native admitted-turn SSE events."""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource
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
    adapter._native_submit_subscribers[REQUEST_REF] = (
        queue, asyncio.get_running_loop(),
    )
    adapter.__dict__.setdefault("_native_queued_submit_refs", set()).add(REQUEST_REF)
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
async def test_native_tool_failure_callback_emits_bounded_failed_event(adapter):
    queue = asyncio.Queue(maxsize=32)
    adapter._native_submit_subscribers[REQUEST_REF] = (
        queue, asyncio.get_running_loop(),
    )
    adapter._native_submit_active_refs["native-session-key"] = REQUEST_REF

    adapter._native_submit_tool_completed(
        "native-session-key",
        "call-failed",
        "search",
        '{"success": false, "error": "File not found"}',
    )

    assert queue.get_nowait() == {
        "native_request_ref": REQUEST_REF,
        "sequence": 1,
        "type": "tool.failed",
        "tool_call_id": "call-failed",
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
    adapter._native_submit_subscribers[REQUEST_REF] = (
        queue, asyncio.get_running_loop(),
    )
    adapter.__dict__.setdefault("_native_queued_submit_refs", set()).add(REQUEST_REF)
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
        adapter._native_submit_close(REQUEST_REF, "turn.completed")
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
async def test_reconnect_replaces_old_subscriber_and_seeds_one_clarification(adapter):
    adapter._native_submit_active_refs["native-session-key"] = REQUEST_REF
    await adapter.send_clarify(
        chat_id=SESSION_ID,
        question="Pick one",
        choices=["A", "B"],
        clarify_id="clarify-1",
        session_key="native-session-key",
    )
    first_client = await _client(adapter)
    second_client = await _client(adapter)
    try:
        first = await first_client.get(
            f"/api/sessions/{SESSION_ID}/submit/{REQUEST_REF}/events",
            headers={"Authorization": "Bearer sk-native-events-test"},
        )
        assert await asyncio.wait_for(first.content.readline(), timeout=1) == b"event: clarify.request\n"
        await first.content.readline()
        await first.content.readline()

        second = await second_client.get(
            f"/api/sessions/{SESSION_ID}/submit/{REQUEST_REF}/events",
            headers={"Authorization": "Bearer sk-native-events-test"},
        )
        assert await asyncio.wait_for(first.content.readline(), timeout=1) == b""
        adapter._native_submit_close(REQUEST_REF, "turn.completed")
        lines = await asyncio.wait_for(second.content.read(), timeout=1)
    finally:
        await first_client.close()
        await second_client.close()

    payloads = [
        json.loads(line.removeprefix(b"data: "))
        for line in lines.splitlines() if line.startswith(b"data: ")
    ]
    assert [payload["type"] for payload in payloads] == [
        "clarify.request", "turn.completed",
    ]
    assert payloads[0]["clarify_id"] == "clarify-1"


@pytest.mark.asyncio
async def test_slow_subscriber_is_released_without_retaining_event_bodies(adapter):
    queue = asyncio.Queue(maxsize=1)
    adapter._native_submit_subscribers[REQUEST_REF] = (
        queue, asyncio.get_running_loop(),
    )

    adapter._native_submit_event(REQUEST_REF, "turn.started")
    adapter._native_submit_event(REQUEST_REF, "assistant.delta", delta="later")

    assert queue.get_nowait() is None
    assert REQUEST_REF not in adapter._native_submit_subscribers
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


@pytest.mark.asyncio
async def test_idle_start_is_receipt_only_but_queued_start_is_live(adapter):
    adapter._session_db.register_native_session_submit(
        SESSION_ID, external_request_id="start-ruling", message_sha256="0" * 64,
        native_request_ref=REQUEST_REF,
    )
    queue = asyncio.Queue(maxsize=32)
    adapter._native_submit_subscribers[REQUEST_REF] = (
        queue, asyncio.get_running_loop(),
    )
    event = SimpleNamespace(
        metadata={"native_request_ref": REQUEST_REF},
        source=SimpleNamespace(chat_id=SESSION_ID, profile=None),
    )

    await adapter._on_native_submit_started(event, "idle-key")
    assert queue.empty()

    adapter._native_submit_active_refs.pop("idle-key", None)
    adapter.__dict__.setdefault("_native_queued_submit_refs", set()).add(REQUEST_REF)
    await adapter._on_native_submit_started(event, "queued-key")
    assert queue.get_nowait()["type"] == "turn.started"


@pytest.mark.asyncio
async def test_terminal_cache_evicts_old_observer_refs_only(adapter):
    for index in range(1_025):
        ref = f"terminal-{index}"
        adapter._native_submit_ref_sessions[ref] = ("default", SESSION_ID)
        adapter._native_submit_close(ref, "turn.completed")

    assert len(adapter._native_submit_terminals) == 1_024
    assert "terminal-0" not in adapter._native_submit_terminals
    assert "terminal-0" not in adapter._native_submit_ref_sessions
    assert "terminal-1024" in adapter._native_submit_ref_sessions


@pytest.mark.asyncio
async def test_profile_mismatch_and_unknown_request_fail_not_found(adapter):
    adapter._native_submit_ref_sessions["other-profile-ref"] = (
        "other-profile", SESSION_ID,
    )
    client = await _client(adapter)
    headers = {"Authorization": "Bearer sk-native-events-test"}
    try:
        profile_mismatch = await client.get(
            f"/api/sessions/{SESSION_ID}/submit/other-profile-ref/events",
            headers=headers,
        )
        unknown = await client.get(
            f"/api/sessions/{SESSION_ID}/submit/unknown-ref/events",
            headers=headers,
        )
    finally:
        await client.close()

    assert profile_mismatch.status == 404
    assert unknown.status == 404


@pytest.mark.asyncio
async def test_raised_native_execution_marks_terminal_failure(
    adapter, monkeypatch, tmp_path,
):
    source = SessionSource(
        platform=Platform.API_SERVER, chat_id=SESSION_ID, user_id="api_server",
    )
    event = MessageEvent(
        text="raise", source=source, message_id=REQUEST_REF,
        metadata={"native_request_ref": REQUEST_REF},
    )
    runner = gateway_run.GatewayRunner(GatewayConfig())
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._handle_active_session_busy_message = AsyncMock(return_value=False)
    runner._is_session_run_current = lambda *_args: True
    runner._reply_anchor_for_event = lambda _event: None
    runner._get_guild_id = lambda _event: None
    runner.hooks = MagicMock(emit=AsyncMock())
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="native-key", session_id=SESSION_ID,
        created_at=datetime.now(), updated_at=datetime.now(),
        platform=Platform.API_SERVER, chat_type="dm",
    )
    runner.session_store.load_transcript.return_value = []
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.has_platform_message_id.return_value = False

    class RaisingAgent:
        def run_conversation(self):
            raise RuntimeError("native boom")

    async def raised_run(**_kwargs):
        return RaisingAgent().run_conversation()

    runner._run_agent = raised_run
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    queue = asyncio.Queue(maxsize=32)
    adapter._native_submit_subscribers[REQUEST_REF] = (
        queue, asyncio.get_running_loop(),
    )

    await runner._handle_message_with_agent(event, source, "native-key", 1)
    await adapter._on_native_submit_finished(event, "native-key")

    assert event.metadata["native_submit_failed"] is True
    assert queue.get_nowait()["type"] == "turn.failed"
    assert queue.get_nowait() is None


@pytest.mark.asyncio
async def test_native_profile_submit_handler_persists_fresh_turn_in_admitted_profile(
    tmp_path, monkeypatch,
):
    """A native /p/general turn keeps its transcript out of root state.db."""
    import hermes_state
    from pathlib import Path

    root = tmp_path / ".hermes"
    general = root / "profiles" / "general"
    root.mkdir()
    general.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH,
    )

    profile_db = SessionDB(general / "state.db")
    profile_db.create_session(SESSION_ID, "api_server")
    runner = gateway_run.GatewayRunner(GatewayConfig(multiplex_profiles=True))
    event = MessageEvent(
        text="hello",
        source=SessionSource(
            platform=Platform.API_SERVER,
            chat_id=SESSION_ID,
            chat_type="dm",
            user_id="api_server",
            profile="general",
        ),
        message_id=REQUEST_REF,
        metadata={"native_submit_authenticated": True},
    )

    async def append_fresh_turn(_event):
        for message in (
            {"role": "session_meta", "tools": [], "model": "hermes"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ):
            await runner.async_session_store.append_to_transcript(SESSION_ID, message)

    runner._handle_message = append_fresh_turn
    root_db = SessionDB(root / "state.db")
    try:
        await runner._make_default_profile_message_handler()(event)
        assert [row["role"] for row in profile_db.get_messages(SESSION_ID)] == [
            "session_meta", "user", "assistant",
        ]
        assert root_db.get_session(SESSION_ID) is None
        assert root_db.get_messages(SESSION_ID) == []
    finally:
        root_db.close()
        profile_db.close()
        runner.session_store.close_all_db_handles()
