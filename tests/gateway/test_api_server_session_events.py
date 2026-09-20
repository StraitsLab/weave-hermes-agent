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
@pytest.mark.parametrize("provider_failed", [True, False], ids=["provider-failure", "success"])
async def test_returned_native_result_terminal(adapter, monkeypatch, tmp_path, provider_failed):
    source = SessionSource(
        platform=Platform.API_SERVER, chat_id=SESSION_ID, user_id="api_server",
    )
    event = MessageEvent(
        text="hello", source=source, message_id=REQUEST_REF,
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
    runner.session_store.has_platform_message_id.return_value = False
    result = {"final_response": "Hello", "messages": [], "api_calls": 0, "tools": []}
    if provider_failed:
        result.update(
            final_response="⚠️ Provider authentication failed: no provider configured",
            failed=True, error="provider_unavailable: no provider configured",
        )
    runner._run_agent = AsyncMock(return_value=result)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    queue = asyncio.Queue(maxsize=32)
    adapter._native_submit_subscribers[REQUEST_REF] = (
        queue, asyncio.get_running_loop(),
    )

    await runner._handle_message_with_agent(event, source, "native-key", 1)
    await adapter._on_native_submit_finished(event, "native-key")

    runner._run_agent.assert_awaited_once()
    if provider_failed:
        assert event.metadata["native_submit_failed"] is True
    else:
        assert "native_submit_failed" not in event.metadata
    assert queue.get_nowait()["type"] == ("turn.failed" if provider_failed else "turn.completed")
    assert queue.get_nowait() is None
    assert queue.empty()


def test_provider_resolution_failure_returns_failed_result():
    """The real TurnRunner except-branch marks the synthetic reply as a failed turn."""
    runner = SimpleNamespace(
        _get_system_prompt_for_channel=lambda *_a, **_k: "",
        _resolve_session_agent_runtime=MagicMock(
            side_effect=RuntimeError("no provider configured for session"),
        ),
    )
    source = SessionSource(platform=Platform.API_SERVER, chat_id=SESSION_ID, user_id="api_server")
    ctx = SimpleNamespace(
        source=source, session_key="native-key", user_config={},
        message="hello", context_prompt="", channel_prompt="",
    )
    result = gateway_run.TurnRunner(runner, ctx).run_sync()

    assert result["failed"] is True
    assert result["error"] == "provider_unavailable: no provider configured for session"
    assert result["final_response"].startswith("⚠️ Provider authentication failed: ")
    assert result["messages"] == [] and result["api_calls"] == 0


# WEV-1726 — a queued native submit is its own turn: attributed by its own ref, closed honestly.


def _subscribe(adapter, ref, external_request_id):
    adapter._session_db.register_native_session_submit(
        SESSION_ID, external_request_id=external_request_id, message_sha256="0" * 64, native_request_ref=ref)
    adapter._native_submit_external_ids[ref] = external_request_id
    queue = asyncio.Queue(maxsize=32)
    adapter._native_submit_subscribers[ref] = (queue, asyncio.get_running_loop())
    adapter._native_submit_ref_sessions[ref] = ("default", SESSION_ID)
    return queue


def _drain(queue):
    events = []
    while not queue.empty():
        item = queue.get_nowait()
        if item is not None:
            events.append(item)
    return events


@pytest.mark.asyncio
async def test_final_names_the_admission_it_answers(adapter):
    """The final carries external_request_id so a consumer attributes by id, never by arrival order."""
    queue = _subscribe(adapter, REQUEST_REF, "handoff-A")
    event = SimpleNamespace(metadata={"native_request_ref": REQUEST_REF}, source=SimpleNamespace(chat_id=SESSION_ID, profile=None))
    await adapter._on_native_submit_started(event, "k")
    adapter._native_submit_final("k", "answer")
    await adapter._on_native_submit_finished(event, "k")
    final = next(e for e in _drain(queue) if e["type"] == "assistant.final")
    assert final["external_request_id"] == "handoff-A" and final["content"] == "answer"


@pytest.mark.asyncio
async def test_queued_followup_switches_attribution_and_terminates_its_own_ref(adapter):
    """Founder call 2026-09-20 12:22Z: a second submit queued behind a running turn ran as its own
    turn but every event was stamped with the FIRST ref and the second never terminated. The in-band
    follow-up must bracket itself with the same start/finish hooks the outer event got."""
    first, second = REQUEST_REF, "native-request-ref-2"
    q1, q2 = _subscribe(adapter, first, "handoff-A"), _subscribe(adapter, second, "handoff-B")
    e1 = SimpleNamespace(metadata={"native_request_ref": first}, source=SimpleNamespace(chat_id=SESSION_ID, profile=None))
    e2 = SimpleNamespace(metadata={"native_request_ref": second}, source=SimpleNamespace(chat_id=SESSION_ID, profile=None))
    adapter.__dict__.setdefault("_native_queued_submit_refs", set()).add(second)

    # REAL nesting (reviewer correction): the follow-up runs INSIDE the outer _run_agent, so the
    # outer finish hook fires AFTER the follow-up has started and finished.
    await adapter._on_native_submit_started(e1, "k")
    adapter._native_submit_final("k", "Here are the headlines.")
    await adapter._on_native_submit_started(e2, "k")          # run.py _begin_native_followup
    adapter._native_submit_final("k", "Hi Molly, good to meet you!")
    await adapter._on_native_submit_finished(e2, "k")         # run.py _end_native_followup
    await adapter._on_native_submit_finished(e1, "k")         # base.py outer finish, last

    ev1, ev2 = _drain(q1), _drain(q2)
    assert [e["type"] for e in ev1] == ["assistant.final", "turn.completed"]
    assert ev1[0]["content"] == "Here are the headlines." and ev1[0]["external_request_id"] == "handoff-A"
    assert [e["type"] for e in ev2] == ["turn.started", "assistant.final", "turn.completed"]
    assert ev2[1]["content"] == "Hi Molly, good to meet you!" and ev2[1]["external_request_id"] == "handoff-B"
    assert first in adapter._native_submit_terminals and second in adapter._native_submit_terminals
    assert adapter._native_submit_active_refs.get("k") is None, "no leaked active ref"


@pytest.mark.asyncio
async def test_text_merged_sibling_is_closed_as_merged_into_the_survivor(adapter):
    """When a third submit's text is folded into an already-queued one, the folded ref would vanish.
    merge_pending_message_event records it on the survivor; the survivor's start closes it."""
    from gateway.platforms.base import merge_pending_message_event, MessageType
    survivor_ref, folded_ref = "native-survivor", "native-folded"
    qs, qf = _subscribe(adapter, survivor_ref, "handoff-S"), _subscribe(adapter, folded_ref, "handoff-F")
    adapter.__dict__.setdefault("_native_queued_submit_refs", set()).update({survivor_ref, folded_ref})
    src = SimpleNamespace(chat_id=SESSION_ID, profile=None)
    survivor = MessageEvent(text="first", message_type=MessageType.TEXT, user_id="u", user_name="u", source=src,
                            message_id=survivor_ref, metadata={"native_request_ref": survivor_ref})
    folded = MessageEvent(text="second", message_type=MessageType.TEXT, user_id="u", user_name="u", source=src,
                          message_id=folded_ref, metadata={"native_request_ref": folded_ref})
    pending = {"k": survivor}
    merge_pending_message_event(pending, "k", folded, merge_text=True)
    assert pending["k"] is survivor and survivor.text == "first\nsecond"
    assert survivor.metadata["merged_native_request_refs"] == [folded_ref]

    await adapter._on_native_submit_started(survivor, "k")
    ef = _drain(qf)
    assert [(e["type"], e.get("merged_into")) for e in ef] == [("turn.started", survivor_ref), ("turn.completed", survivor_ref)]
    assert folded_ref in adapter._native_submit_terminals
    assert [e["type"] for e in _drain(qs)] == ["turn.started"]
    # Idempotent: a second start (retry) does not re-close the folded ref.
    await adapter._on_native_submit_started(survivor, "k")
    assert _drain(qf) == []


@pytest.mark.asyncio
async def test_run_followup_brackets_a_native_event_and_ignores_others(monkeypatch):
    """gateway_run._begin/_end_native_followup call the adapter hooks only for native events."""
    runner = gateway_run.GatewayRunner.__new__(gateway_run.GatewayRunner)
    calls = []
    class Adapter:
        async def _on_native_submit_started(self, event, key): calls.append(("start", event.metadata["native_request_ref"], key))
        async def _on_native_submit_finished(self, event, key): calls.append(("finish", event.metadata["native_request_ref"], key, event.metadata.get("native_submit_failed")))
    monkeypatch.setattr(runner, "_adapter_for_source", lambda source: Adapter(), raising=False)
    native = SimpleNamespace(metadata={"native_request_ref": "r1"}, source=object())
    plain = SimpleNamespace(metadata={}, source=object())
    assert await runner._begin_native_followup(plain, "k") is None
    await runner._end_native_followup(None, "k", failed=False)
    assert calls == []
    token = await runner._begin_native_followup(native, "k")
    await runner._end_native_followup(token, "k", failed=True)
    assert calls == [("start", "r1", "k"), ("finish", "r1", "k", True)]


@pytest.mark.asyncio
async def test_run_followup_start_failure_closes_the_ref_and_spares_the_outer_turn(monkeypatch):
    """The start hook installs the ref as active BEFORE its db await. If that await fails, the
    follow-up is closed as failed and NOT run — and the failure must not escape into the outer
    turn, whose answer was already produced and delivered (second review: re-raising marked A failed)."""
    runner = gateway_run.GatewayRunner.__new__(gateway_run.GatewayRunner)
    calls = []
    class Adapter:
        async def _on_native_submit_started(self, event, key): calls.append("start"); raise RuntimeError("db down")
        async def _on_native_submit_finished(self, event, key): calls.append(("finish", event.metadata.get("native_submit_failed")))
    monkeypatch.setattr(runner, "_adapter_for_source", lambda source: Adapter(), raising=False)
    native = SimpleNamespace(metadata={"native_request_ref": "r1"}, source=object())
    token = await runner._begin_native_followup(native, "k")          # does not raise
    assert token is None and runner._native_followup_skipped(native)
    assert calls == ["start", ("finish", True)]


@pytest.mark.asyncio
async def test_run_followup_cancelled_during_start_still_closes_the_ref(monkeypatch):
    """Cancellation while the start hook awaits its db update: the ref was already installed as
    active; it must be closed as failed, and the cancellation must keep propagating."""
    runner = gateway_run.GatewayRunner.__new__(gateway_run.GatewayRunner)
    calls = []
    class Adapter:
        async def _on_native_submit_started(self, event, key): calls.append("start"); raise asyncio.CancelledError()
        async def _on_native_submit_finished(self, event, key): calls.append(("finish", event.metadata.get("native_submit_failed")))
    monkeypatch.setattr(runner, "_adapter_for_source", lambda source: Adapter(), raising=False)
    native = SimpleNamespace(metadata={"native_request_ref": "r1"}, source=object())
    with pytest.raises(asyncio.CancelledError):
        await runner._begin_native_followup(native, "k")
    assert calls == ["start", ("finish", True)]


@pytest.mark.asyncio
async def test_api_start_failure_after_activation_is_closed_by_end(adapter):
    """End-to-end on the real adapter: the start hook activates the ref then its db update fails;
    _end_native_followup(failed=True) must emit turn.failed and release the active ref."""
    queue = _subscribe(adapter, REQUEST_REF, "handoff-A")
    event = SimpleNamespace(metadata={"native_request_ref": REQUEST_REF}, source=SimpleNamespace(chat_id=SESSION_ID, profile=None))
    def _boom(**kwargs): raise RuntimeError("db down")
    adapter._session_db.set_native_session_submit_admission = _boom  # the await after activation fails
    runner = gateway_run.GatewayRunner.__new__(gateway_run.GatewayRunner)
    runner._adapter_for_source = lambda source: adapter
    token = await runner._begin_native_followup(event, "k")
    await asyncio.sleep(0)
    assert token is None
    assert adapter._native_submit_active_refs.get("k") is None, "activated ref must be released"
    assert REQUEST_REF in adapter._native_submit_terminals
    assert [e["type"] for e in _drain(queue)][-1] == "turn.failed"


@pytest.mark.asyncio
async def test_final_omits_external_request_id_when_unknown(adapter):
    """No null on the wire: an unknown id is simply absent, so a consumer uses its fallback rule."""
    queue = asyncio.Queue(maxsize=32)
    adapter._native_submit_subscribers[REQUEST_REF] = (queue, asyncio.get_running_loop())
    adapter._native_submit_active_refs["k"] = REQUEST_REF
    adapter._native_submit_external_ids.pop(REQUEST_REF, None)
    adapter._native_submit_final("k", "x")
    await asyncio.sleep(0)
    event = queue.get_nowait()
    assert event["type"] == "assistant.final" and "external_request_id" not in event


def test_media_fold_also_records_the_folded_native_ref():
    """Photo/media folds absorb an event too; the folded ref must survive on the survivor."""
    from gateway.platforms.base import merge_pending_message_event, MessageType
    src = SimpleNamespace(chat_id=SESSION_ID, profile=None)
    survivor = MessageEvent(text="", message_type=MessageType.PHOTO, user_id="u", user_name="u", source=src,
                            message_id="s", media_urls=["a.jpg"], media_types=["image/jpeg"], metadata={"native_request_ref": "ref-s"})
    folded = MessageEvent(text="", message_type=MessageType.PHOTO, user_id="u", user_name="u", source=src,
                          message_id="f", media_urls=["b.jpg"], media_types=["image/jpeg"],
                          metadata={"native_request_ref": "ref-f", "merged_native_request_refs": ["ref-older"]})
    pending = {"k": survivor}
    merge_pending_message_event(pending, "k", folded)
    assert survivor.metadata["merged_native_request_refs"] == ["ref-f", "ref-older"]
    # Never orphaned: every admitted ref is kept, however many fold in (second review: a cap
    # silently dropped later refs). Growth is bounded by the admission path, not here.
    for i in range(100):
        merge_pending_message_event(pending, "k", MessageEvent(text="", message_type=MessageType.PHOTO, user_id="u", user_name="u", source=src,
            message_id=str(i), media_urls=["c.jpg"], media_types=["image/jpeg"], metadata={"native_request_ref": f"ref-{i}"}))
    refs = survivor.metadata["merged_native_request_refs"]
    assert len(refs) == 102 and refs[-1] == "ref-99" and len(set(refs)) == 102
