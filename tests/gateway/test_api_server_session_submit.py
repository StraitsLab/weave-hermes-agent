"""Behavior contracts for native gateway-process session admission."""

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.run import GatewayRunner
import gateway.run as gateway_run
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
    adapter.gateway_runner = SimpleNamespace(
        _running=True, session_credential_available=lambda *_args: True,
    )
    try:
        yield adapter
    finally:
        db.close()


async def _client(adapter):
    app = web.Application()
    app.router.add_get("/api/sessions/{session_id}/messages", adapter._handle_session_messages)
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

    async def admit(session_id, message, native_request_ref, external_request_id=""):
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
async def test_submit_requires_a_bound_session_credential_before_native_admission(
    adapter, tmp_path, monkeypatch,
):
    """An ambient provider key cannot admit an unbound native REST session."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key-must-not-admit")
    runner = GatewayRunner(GatewayConfig())
    runner._running = True
    adapter._session_db = runner._session_db._db
    adapter._session_db.create_session(SESSION_ID, "api_server")
    adapter.gateway_runner = runner
    admitted = []

    async def admit(*args):
        admitted.append(args)
        return "streaming"

    monkeypatch.setattr(adapter, "_admit_native_session_submit", admit)
    client = await _client(adapter)
    try:
        response = await client.post(
            f"/api/sessions/{SESSION_ID}/submit",
            headers={"Authorization": "Bearer sk-native-submit-test"},
            json=_request("unbound-request"),
        )
        body = await response.json()
    finally:
        await client.close()
        runner._session_db._db.close()

    assert response.status == 409
    assert body["error"]["code"] == "credential_unavailable"
    assert admitted == []


@pytest.mark.asyncio
async def test_submit_rejects_changed_retry_and_non_queue_busy_modes(adapter, monkeypatch):
    async def admit(session_id, message, native_request_ref, external_request_id=""):
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
            json={**_request("request-2"), "busy_mode": "stop"},
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

    async def admit(session_id, message, native_request_ref, external_request_id=""):
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

    assert await adapter._admit_native_session_submit(
        SESSION_ID, "first", "native-1", external_request_id="writer-request") == "streaming"
    event = adapter.handle_message.await_args.args[0]
    assert adapter._native_submit_ref_sessions["native-1"] == ("default", SESSION_ID)
    assert event.internal is False
    assert event.metadata["native_submit_authenticated"] is True
    assert event.metadata["gateway_session_strict"] is True
    assert event.metadata["native_request_ref"] == "native-1"
    # Weave: the caller's request id rides the event so the turn it starts adopts it as its turn id.
    assert event.metadata["external_request_id"] == "writer-request"

    adapter._active_sessions["native-key"] = asyncio.Event()
    assert await adapter._admit_native_session_submit(
        SESSION_ID, "second", "native-2", external_request_id="second-request") == "queued"
    assert [(key, event.text) for key, event in queued] == [("native-key", "second")]
    # A queued submit keeps ITS OWN request id on its event; nothing is staged on the session yet.
    assert queued[0][1].metadata["external_request_id"] == "second-request"
    assert adapter.handle_message.await_count == 1


@pytest.mark.asyncio
async def test_native_submit_fails_closed_while_gateway_is_draining(adapter):
    adapter.gateway_runner = SimpleNamespace(_running=True, _draining=True)

    with pytest.raises(RuntimeError, match="unavailable"):
        await adapter._admit_native_session_submit(SESSION_ID, "hello", "native-drain")


@pytest.mark.asyncio
async def test_native_submit_fails_closed_during_startup_restore(adapter, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = GatewayRunner(GatewayConfig())
    runner._running = True
    runner._startup_restore_in_progress = True
    adapter.gateway_runner = runner
    adapter.handle_message = AsyncMock()
    try:
        with pytest.raises(RuntimeError, match="unavailable"):
            await adapter._admit_native_session_submit(SESSION_ID, "hello", "native-startup")
        adapter.handle_message.assert_not_awaited()
        assert runner._startup_restore_queue == []
    finally:
        runner._session_db._db.close()


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


@pytest.mark.asyncio
async def test_submit_passes_the_external_request_id_into_native_admission(adapter, monkeypatch):
    """The route hands the caller's request id (weave-api's Ledger command id) to admission unchanged."""
    admitted = []

    async def admit(session_id, message, native_request_ref, external_request_id=""):
        admitted.append((session_id, message, native_request_ref, external_request_id))
        return "streaming"

    monkeypatch.setattr(adapter, "_admit_native_session_submit", admit)
    client = await _client(adapter)
    try:
        response = await client.post(f"/api/sessions/{SESSION_ID}/submit", json=_request("cmd-01a0-7f00"),
                                     headers={"Authorization": "Bearer sk-native-submit-test"})
        assert response.status == 202
    finally:
        await client.close()
    assert [item[3] for item in admitted] == ["cmd-01a0-7f00"]


def _busy_adapter(adapter, agent):
    """A busy native session whose running turn is ``native-running`` and whose live agent is ``agent``."""
    entry = SimpleNamespace(session_key="native-key", session_id=SESSION_ID)
    queued = []
    runner = SimpleNamespace(
        _running=True,
        async_session_store=SimpleNamespace(bind_existing_session=AsyncMock(return_value=entry)),
        _is_session_running=lambda key: True,
        _enqueue_fifo=lambda key, event, adapter: queued.append(event.text),
        _peek_session_state=lambda key: SimpleNamespace(turn=SimpleNamespace(agent=agent)),
    )
    adapter.gateway_runner = runner
    adapter._native_submit_active_refs["native-key"] = "native-running"
    return queued


class _Agent:
    def __init__(self, steer_accepts=True):
        self.steered, self.interrupted, self._accepts = [], [], steer_accepts

    def steer(self, text):
        self.steered.append(text)
        return self._accepts

    def interrupt(self, message=None, **_kwargs):
        self.interrupted.append(message)


@pytest.mark.asyncio
async def test_steer_joins_the_running_turn_and_settles_when_that_turn_ends(adapter):
    """WEV-2108: steer reaches the running agent, not the FIFO, and the steered ref stays open
    (observable through its events) until the turn it joined finishes, then ends with it."""
    agent = _Agent()
    queued = _busy_adapter(adapter, agent)

    admission = await adapter._admit_native_session_submit(
        SESSION_ID, "make it Saturday", "native-steered", external_request_id="steer-request", busy_mode="steer")

    assert admission == "steered"
    assert agent.steered == ["make it Saturday"] and queued == [] and agent.interrupted == []
    assert "native-steered" not in adapter._native_submit_terminals

    closed = []
    original = adapter._native_submit_close
    adapter._native_submit_close = lambda ref, kind, **fields: (closed.append((ref, kind, fields.get("steered_into"))),
                                                                original(ref, kind, **fields))
    running = SimpleNamespace(metadata={"native_request_ref": "native-running"})
    await adapter._on_native_submit_finished(running, "native-key")

    assert ("native-steered", "turn.completed", "native-running") in closed
    assert ("native-running", "turn.completed", None) in closed
    assert not any(kind == "turn.failed" for _ref, kind, _into in closed)


@pytest.mark.asyncio
async def test_a_steered_ref_fails_with_the_turn_it_joined(adapter):
    queued = _busy_adapter(adapter, _Agent())
    closed = []
    original = adapter._native_submit_close
    adapter._native_submit_close = lambda ref, kind, **fields: (closed.append((ref, kind)), original(ref, kind, **fields))

    await adapter._admit_native_session_submit(SESSION_ID, "also add Priya", "native-steered", busy_mode="steer")
    running = SimpleNamespace(metadata={"native_request_ref": "native-running", "native_submit_failed": True})
    await adapter._on_native_submit_finished(running, "native-key")

    assert ("native-steered", "turn.failed") in closed and queued == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["absent", "still-starting", "steer-rejected"])
async def test_steer_the_agent_cannot_take_is_queued_not_lost(adapter, state):
    from gateway.run import _AGENT_PENDING_SENTINEL

    agent = {"absent": None, "still-starting": _AGENT_PENDING_SENTINEL,
             "steer-rejected": _Agent(steer_accepts=False)}[state]
    queued = _busy_adapter(adapter, agent)

    admission = await adapter._admit_native_session_submit(SESSION_ID, "make it Saturday", "native-steered",
                                                           busy_mode="steer")

    assert admission == "queued" and queued == ["make it Saturday"]
    assert "native-steered" not in adapter._native_submit_steered.get("native-running", [])


@pytest.mark.asyncio
async def test_interrupt_stops_the_running_turn_and_runs_this_submit_next(adapter):
    """WEV-2108: interrupt queues the submit as its own turn (own ref, own events) and stops the running one."""
    agent = _Agent()
    queued = _busy_adapter(adapter, agent)

    admission = await adapter._admit_native_session_submit(SESSION_ID, "forget that, book the dentist",
                                                           "native-new", busy_mode="interrupt")

    assert admission == "queued"
    assert queued == ["forget that, book the dentist"]
    assert agent.interrupted == ["forget that, book the dentist"] and agent.steered == []


@pytest.mark.asyncio
async def test_queue_mode_never_touches_the_running_agent(adapter):
    agent = _Agent()
    queued = _busy_adapter(adapter, agent)

    assert await adapter._admit_native_session_submit(SESSION_ID, "later", "native-q", busy_mode="queue") == "queued"
    assert queued == ["later"] and agent.steered == [] and agent.interrupted == []


@pytest.mark.asyncio
async def test_steered_retry_after_admission_write_failure_steers_once(adapter, monkeypatch):
    agent = _Agent()
    queued = _busy_adapter(adapter, agent)
    adapter.gateway_runner.session_credential_available = lambda *_args: True
    original = adapter._session_db.set_native_session_submit_admission
    writes = []

    def write(**kwargs):
        writes.append(kwargs)
        if len(writes) == 1:
            raise RuntimeError("admission write failed once")
        return original(**kwargs)

    monkeypatch.setattr(adapter._session_db, "set_native_session_submit_admission", write)
    status, body = await _post_submit(adapter, "steered-write-failure", "steer")
    assert status == 503 and body["error"]["code"] == "native_admission_unavailable"
    status, receipt = await _post_submit(adapter, "steered-write-failure", "steer")
    assert status == 202 and receipt["admission"] == "steered"
    assert receipt["native_request_ref"] == writes[0]["native_request_ref"]
    assert agent.steered == ["hello"] and queued == agent.interrupted == []


@pytest.mark.asyncio
async def test_submit_route_passes_busy_mode_and_replays_a_steered_admission(adapter, monkeypatch):
    admitted = []

    async def admit(session_id, message, native_request_ref, external_request_id="", busy_mode="queue"):
        admitted.append(busy_mode)
        return "steered"

    monkeypatch.setattr(adapter, "_admit_native_session_submit", admit)
    client = await _client(adapter)
    headers = {"Authorization": "Bearer sk-native-submit-test"}
    try:
        first = await client.post(f"/api/sessions/{SESSION_ID}/submit", headers=headers,
                                  json={**_request("steer-1"), "busy_mode": "steer"})
        retry = await client.post(f"/api/sessions/{SESSION_ID}/submit", headers=headers,
                                  json={**_request("steer-1"), "busy_mode": "steer"})
        first_body, retry_body = await first.json(), await retry.json()
    finally:
        await client.close()

    assert admitted == ["steer"]
    assert first.status == retry.status == 202
    assert first_body["admission"] == retry_body["admission"] == "steered"
    assert first_body["native_request_ref"] == retry_body["native_request_ref"]


@pytest.mark.asyncio
@pytest.mark.parametrize("busy_mode", [["steer"], {"steer": True}])
async def test_a_non_string_busy_mode_is_a_schema_error_not_a_crash(adapter, busy_mode):
    """WEV-2108: a JSON list/object busy_mode is rejected as 400, not a 500 from an unhashable set lookup."""
    client = await _client(adapter)
    try:
        response = await client.post(f"/api/sessions/{SESSION_ID}/submit", headers={"Authorization": f"Bearer {adapter.config.extra['key']}"},
                                     json={**_request("bad-mode"), "busy_mode": busy_mode})
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 400
    assert body["error"]["code"] == "invalid_native_submit_schema"


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["subagents", "compression"])
async def test_interrupt_is_demoted_to_queue_while_the_gateway_would_demote_it(adapter, guard):
    """WEV-2108: like the gateway's own interrupt paths (#30170, #56391), a native interrupt never stops a turn
    driving subagents or mid-compression; the submit still runs next as its own queued turn."""
    agent = _Agent()
    agent._active_children = [object()] if guard == "subagents" else []
    queued = _busy_adapter(adapter, agent)
    adapter.gateway_runner._session_has_compression_in_flight = AsyncMock(return_value=guard == "compression")

    admission = await adapter._admit_native_session_submit(SESSION_ID, "book the dentist", "native-new",
                                                           busy_mode="interrupt")

    assert admission == "queued" and queued == ["book the dentist"]
    assert agent.interrupted == []


async def _post_submit(adapter, request_id, busy_mode, message="hello", **extra):
    adapter.gateway_runner.session_credential_available = lambda *_args: True
    client = await _client(adapter)
    try:
        response = await client.post(f"/api/sessions/{SESSION_ID}/submit",
                                     headers={"Authorization": f"Bearer {adapter.config.extra['key']}"},
                                     json={**_request(request_id, message), "busy_mode": busy_mode, **extra})
        return response.status, await response.json()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_interrupt_through_the_http_route_reaches_the_running_agent(adapter):
    """F4(a): the real route carries busy_mode=interrupt to the running agent, and the submit still queues."""
    agent = _Agent()
    queued = _busy_adapter(adapter, agent)

    status, body = await _post_submit(adapter, "interrupt-wire", "interrupt")

    assert status == 202 and body["admission"] == "queued"
    assert agent.interrupted == ["hello"] and queued == ["hello"] and agent.steered == []


@pytest.mark.asyncio
@pytest.mark.parametrize("first,second", [(a, b) for a in ("queue", "steer", "interrupt")
                                          for b in ("queue", "steer", "interrupt") if a != b])
async def test_a_retry_that_changes_busy_mode_is_changed_input(adapter, first, second):
    """F3: busy_mode is part of the request identity. A changed mode is a 409, never a replay."""
    agent = _Agent()
    _busy_adapter(adapter, agent)

    first_status, _ = await _post_submit(adapter, "mode-change", first)
    retry_status, retry = await _post_submit(adapter, "mode-change", second)

    assert first_status == 202
    assert retry_status == 409 and retry["error"]["code"] == "native_submit_idempotency_conflict"


@pytest.mark.asyncio
async def test_a_queue_record_from_before_busy_modes_still_replays(adapter):
    """F3: records written before busy modes keep the message-only hash, so their identical retry replays."""
    adapter._session_db.register_native_session_submit(
        SESSION_ID, external_request_id="legacy", native_request_ref="legacy-ref",
        message_sha256=hashlib.sha256(b"hello").hexdigest())
    adapter._session_db.set_native_session_submit_admission(native_request_ref="legacy-ref", admission="queued")
    _busy_adapter(adapter, _Agent())

    status, body = await _post_submit(adapter, "legacy", "queue")

    assert status == 202 and body["native_request_ref"] == "legacy-ref"


def _real_agent():
    """The real AIAgent steer/drain/window owners, without a model."""
    import threading
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent._pending_steer_lock, agent._pending_steer, agent._execution_thread_id = threading.Lock(), None, None
    return agent


@pytest.mark.asyncio
async def test_a_steer_after_the_turn_took_its_last_leftover_is_queued_not_lost(adapter):
    """F1(b): once the finalizer has taken the leftover, a steer would never be read. The agent refuses it,
    so the submit queues as its own turn instead of being reported steered and dropped."""
    agent = _real_agent()
    queued = _busy_adapter(adapter, agent)

    leftover = agent._close_steer_window()  # turn_finalizer's single take
    admission = await adapter._admit_native_session_submit(SESSION_ID, "do not book it", "late-steer",
                                                           busy_mode="steer")

    assert leftover is None
    assert admission == "queued" and queued == ["do not book it"] and agent._pending_steer is None
    agent._open_steer_window()  # the next turn accepts steers again
    assert agent.steer_if_open("make it Saturday") is True and agent._drain_pending_steer() == "make it Saturday"


def test_a_steer_accepted_before_the_window_closes_is_the_leftover():
    agent = _real_agent()
    assert agent.steer_if_open("make it Saturday") is True
    assert agent._close_steer_window() == "make it Saturday"
    assert agent.steer_if_open("too late") is False


def test_plain_steer_keeps_its_contract_for_other_callers_after_the_window_closes():
    """R2-F1: CLI, TUI, ACP and gateway /steer treat False as empty input, so steer() itself never refuses
    non-empty text; only native submit (which queues on refusal) uses steer_if_open."""
    agent = _real_agent()
    agent._close_steer_window()
    assert agent.steer("do not book it") is True and agent._pending_steer == "do not book it"
    assert agent.steer("   ") is False


@pytest.mark.asyncio
async def test_changed_mode_and_text_cannot_alias_a_queue_identity(adapter):
    """R2-F3: a queue message that spells another mode's encoding is still a different request."""
    agent = _Agent()
    _busy_adapter(adapter, agent)

    first, _ = await _post_submit(adapter, "alias", "queue", message="hello\u0000busy_mode=interrupt")
    for crafted in ("interrupt:" + hashlib.sha256(b"hello").hexdigest(), "hello"):
        status, body = await _post_submit(adapter, "alias", "interrupt", message=crafted)
        assert status == 409 and body["error"]["code"] == "native_submit_idempotency_conflict"
    assert first == 202 and agent.interrupted == []


@pytest.mark.asyncio
async def test_an_identical_steer_or_interrupt_retry_replays(adapter):
    agent = _Agent()
    _busy_adapter(adapter, agent)
    for mode in ("steer", "interrupt"):
        first, one = await _post_submit(adapter, f"same-{mode}", mode)
        retry, two = await _post_submit(adapter, f"same-{mode}", mode)
        assert first == retry == 202 and one["native_request_ref"] == two["native_request_ref"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["turn-ended", "agent-replaced", "subagent-started"])
async def test_an_interrupt_is_dropped_if_its_target_changed_during_the_compression_check(adapter, change):
    """F2: the interrupt is fenced to the turn it was aimed at, re-checked after the compression await,
    so it can never stop its own queued turn, a different agent, or a turn now driving subagents."""
    agent = _Agent()
    agent._active_children = []
    queued = _busy_adapter(adapter, agent)
    runner = adapter.gateway_runner

    async def compression_check(key):
        await asyncio.sleep(0)
        if change == "turn-ended":
            adapter._native_submit_active_refs[key] = "native-new"  # this very submit is now running
        elif change == "agent-replaced":
            runner._peek_session_state = lambda _k: SimpleNamespace(turn=SimpleNamespace(agent=_Agent()))
        else:
            agent._active_children = [object()]
        return False

    runner._session_has_compression_in_flight = compression_check
    admission = await adapter._admit_native_session_submit(SESSION_ID, "new task", "native-new", busy_mode="interrupt")

    assert admission == "queued" and queued == ["new task"]
    assert agent.interrupted == []


@pytest.mark.asyncio
async def test_expected_target_mismatch_has_no_side_effects(adapter):
    agent = _Agent()
    queued = _busy_adapter(adapter, agent)
    for mode in ("steer", "interrupt"):
        result = await adapter._admit_native_session_submit(SESSION_ID, "later", "native-q", busy_mode=mode, expected_active_ref="finished-ref")
        assert result == "target_changed"
        assert queued == [] and agent.steered == [] and agent.interrupted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["steer", "interrupt"])
async def test_expected_target_is_precondition_not_replay_identity(adapter, mode):
    agent = _Agent()
    queued = _busy_adapter(adapter, agent)
    target = next(iter(adapter._native_submit_active_refs.values()))
    first, one = await _post_submit(adapter, "fenced-replay", mode, expected_active_ref=target)
    assert first == 202
    actions = (list(queued), list(agent.steered), list(agent.interrupted))
    second, two = await _post_submit(adapter, "fenced-replay", mode, expected_active_ref="gone")
    assert second == 202 and two == one
    assert actions == (queued, agent.steered, agent.interrupted)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["steer", "interrupt"])
async def test_expected_target_refusal_leaves_no_record_and_can_queue(adapter, mode):
    agent = _Agent()
    queued = _busy_adapter(adapter, agent)
    status, body = await _post_submit(adapter, "fenced-refusal", mode, expected_active_ref="unrelated-native-ref")
    assert status == 409 and body["error"]["code"] == "native_submit_target_changed"
    assert queued == agent.steered == agent.interrupted == []
    status, body = await _post_submit(adapter, "fenced-refusal", "queue")
    assert status == 202 and body["admission"] == "queued"
    assert queued == ["hello"]


@pytest.mark.asyncio
async def test_expected_target_fence_after_compression_await(adapter):
    agent = _Agent()
    agent._active_children = []
    queued = _busy_adapter(adapter, agent)
    target = next(iter(adapter._native_submit_active_refs.values()))
    async def compression(key):
        await asyncio.sleep(0)
        adapter._native_submit_active_refs[key] = "unrelated-native-ref"
        return False
    adapter.gateway_runner._session_has_compression_in_flight = compression
    status, body = await _post_submit(adapter, "fenced-await", "interrupt", expected_active_ref=target)
    assert status == 409 and body["error"]["code"] == "native_submit_target_changed"
    assert queued == agent.steered == agent.interrupted == []
    status, _ = await _post_submit(adapter, "fenced-await", "queue")
    assert status == 202 and queued == ["hello"]


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [None, "", 3, True, [], {}])
async def test_expected_target_schema_rejects_invalid_values(adapter, target):
    status, body = await _post_submit(adapter, "bad-target", "steer", expected_active_ref=target)
    assert status == 400 and body["error"]["code"] == "invalid_native_submit_schema"


@pytest_asyncio.fixture
async def internal_gateway(tmp_path, monkeypatch):
    """Real admission, runner, AIAgent and DB; only the provider is a local fixture."""
    requests = []
    started, release = asyncio.Event(), asyncio.Event()
    release.set()

    async def complete(request):
        payload = await request.json()
        assert request.headers["Authorization"] == "Bearer local-provider-key"
        requests.append(payload)
        started.set()
        await release.wait()
        reply = f"reply-{len(requests)}"
        if payload.get("stream"):
            chunks = [
                {"id": "local", "object": "chat.completion.chunk", "created": 0,
                 "model": "local-test", "choices": [{"index": 0, "delta": {"role": "assistant", "content": reply}, "finish_reason": None}]},
                {"id": "local", "object": "chat.completion.chunk", "created": 0,
                 "model": "local-test", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}},
            ]
            body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
            return web.Response(text=body, content_type="text/event-stream")
        return web.json_response({
            "id": "local", "object": "chat.completion", "created": 0,
            "model": "local-test", "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        })

    app = web.Application()
    app.router.add_post("/v1/chat/completions", complete)
    provider = TestServer(app)
    await provider.start_server()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(json.dumps({
        "model": {"default": "local-test", "provider": "custom", "base_url": str(provider.make_url("/v1")), "context_length": 131072},
        "auxiliary": {"title_generation": {"enabled": False}},
        "toolsets": [], "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "display": {"platforms": {"api_server": {"streaming": False}}},
    }), encoding="utf-8")
    runner = GatewayRunner(GatewayConfig())
    runner._running = True
    runner._gateway_loop = asyncio.get_running_loop()
    db = runner._session_db._db
    db.create_session(SESSION_ID, "api_server")
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "local-api-key"}))
    adapter._session_db = db
    adapter.gateway_runner = runner
    adapter.set_session_store(runner.session_store)
    adapter.set_message_handler(runner._handle_message)
    runner.adapters = {Platform.API_SERVER: adapter}
    client = await _client(adapter)
    try:
        yield SimpleNamespace(runner=runner, adapter=adapter, db=db, client=client,
                              requests=requests, started=started, release=release)
    finally:
        release.set()
        await adapter.cancel_background_tasks()
        await client.close()
        await provider.close()
        db.close()


async def _bind_internal_gateway(gateway):
    from gateway.session import SessionSource

    source = SessionSource(platform=Platform.API_SERVER, chat_id=SESSION_ID,
                           chat_type="dm", user_id="api_server", user_name="API server")
    assert gateway.runner.bind_session_credential(
        source, SESSION_ID, "local-provider-key",
        datetime.now(timezone.utc) + timedelta(minutes=5), "local-route")
    return gateway.runner.session_store._generate_session_key(source)


async def _submit_internal_gateway(gateway, request_id, message="Work result", **extra):
    response = await gateway.client.post(
        f"/api/sessions/{SESSION_ID}/submit", headers={"Authorization": "Bearer local-api-key"},
        json={**_request(request_id, message), **extra})
    return response.status, await response.json()


async def _wait_native_terminal(gateway, ref):
    async with asyncio.timeout(20):
        while ref not in gateway.adapter._native_submit_terminals:
            await asyncio.sleep(0.01)
    rows = [row for row in gateway.db.get_messages(SESSION_ID) if row["role"] in {"user", "assistant"}]
    assert any(row["role"] == "assistant" for row in rows), rows
    return rows


@pytest.mark.asyncio
async def test_internal_idle_submit_persists_hidden_user_and_clean_provider_payload(internal_gateway):
    """D-119 3a/3f: actual wire payload and /messages, including resumed history."""
    gateway = internal_gateway
    await _bind_internal_gateway(gateway)
    message = "[Work completed]\n  Preserve this content exactly.  "
    status, receipt = await _submit_internal_gateway(gateway, "internal-idle", message, internal=True)
    assert status == 202, receipt
    assert receipt["admission"] == "streaming"
    rows = await _wait_native_terminal(gateway, receipt["native_request_ref"])
    assert [(row["role"], row["content"]) for row in rows] == [("user", message), ("assistant", "reply-1")]
    assert rows[0]["display_kind"] == "internal_notification"
    assert rows[1].get("display_kind") is None
    response = await gateway.client.get(f"/api/sessions/{SESSION_ID}/messages", headers={"Authorization": "Bearer local-api-key"})
    assert response.status == 200
    messages = (await response.json())["data"]
    assert messages[0]["display_kind"] == "internal_notification"
    assert messages[0]["role"] == "user" and messages[0]["content"] == message
    assert messages[1].get("display_kind") is None
    status, next_receipt = await _submit_internal_gateway(gateway, "after-internal", "ordinary follow-up")
    assert status == 202, next_receipt
    await _wait_native_terminal(gateway, next_receipt["native_request_ref"])
    assert len(gateway.requests) == 2
    for payload in gateway.requests:
        assert all("display_kind" not in item for item in payload["messages"])
        assert any(item["role"] == "user" and message in item["content"] for item in payload["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("busy_mode", ["steer", "interrupt"])
async def test_internal_busy_submit_is_fifo_not_steer_or_interrupt(internal_gateway, busy_mode):
    """D-119 3b: the real running user turn finishes before internal turns start."""
    gateway = internal_gateway
    key = await _bind_internal_gateway(gateway)
    gateway.release.clear()
    status, user_receipt = await _submit_internal_gateway(gateway, "running-user", "user task")
    assert status == 202, user_receipt
    await asyncio.wait_for(gateway.started.wait(), timeout=10)
    # Agent publication is asynchronous; the provider can be reached before
    # the gateway tracking task replaces its startup sentinel.
    async with asyncio.timeout(10):
        while (agent := gateway.adapter._native_submit_running_agent(gateway.runner, key)) is None:
            await asyncio.sleep(0.01)
    receipts = []
    for index in range(2):
        status, receipt = await _submit_internal_gateway(
            gateway, f"internal-busy-{index}", f"Work result {index}", internal=True,
            busy_mode=busy_mode, expected_active_ref="stale-target-ignored-for-internal")
        assert status == 202, receipt
        assert receipt["admission"] == "queued"
        receipts.append(receipt)
    queued = [gateway.adapter._pending_messages[key], *gateway.runner._session_state(key).conversation.queued_events]
    for index, event in enumerate(queued):
        assert event.internal is True and event.allow_gateway_control is False
        assert event.message_id == receipts[index]["native_request_ref"]
        assert event.metadata["native_submit_authenticated"] is True
        assert event.metadata["external_request_id"] == f"internal-busy-{index}"
        assert event.metadata["gateway_session_id"] == SESSION_ID
        assert event.metadata["gateway_session_key"] == key
        assert event.metadata["gateway_session_strict"] is True
    assert len(queued) == 2 and len(gateway.requests) == 1
    assert agent._pending_steer is None and not agent._interrupt_requested
    assert user_receipt["native_request_ref"] not in gateway.adapter._native_submit_terminals
    gateway.release.set()
    for receipt in [user_receipt, *receipts]:
        rows = await _wait_native_terminal(gateway, receipt["native_request_ref"])
    assert [(row["role"], row["content"]) for row in rows] == [
        ("user", "user task"), ("assistant", "reply-1"),
        ("user", "Work result 0"), ("assistant", "reply-2"),
        ("user", "Work result 1"), ("assistant", "reply-3")]
    assert [row.get("display_kind") for row in rows] == [None, None, "internal_notification", None, "internal_notification", None]
    assert len(gateway.requests) == 3
    assert all("display_kind" not in item for payload in gateway.requests for item in payload["messages"])


@pytest.mark.asyncio
async def test_internal_submit_identical_retry_runs_one_real_turn(internal_gateway):
    gateway = internal_gateway
    await _bind_internal_gateway(gateway)
    gateway.release.clear()
    status, receipt = await _submit_internal_gateway(gateway, "internal-retry", internal=True)
    assert status == 202, receipt
    await asyncio.wait_for(gateway.started.wait(), timeout=10)
    retry_status, retry = await _submit_internal_gateway(gateway, "internal-retry", internal=True)
    assert retry_status == 202 and retry == receipt
    gateway.release.set()
    rows = await _wait_native_terminal(gateway, receipt["native_request_ref"])
    terminal_status, terminal_retry = await _submit_internal_gateway(gateway, "internal-retry", internal=True)
    assert terminal_status == 202 and terminal_retry == receipt
    assert len(gateway.requests) == 1 and len(rows) == 2


async def _wait_gateway_idle(gateway):
    async with asyncio.timeout(20):
        while gateway.adapter._background_tasks:
            await asyncio.sleep(0.01)
    return [row for row in gateway.db.get_messages(SESSION_ID)
            if row["role"] in {"user", "assistant"}]


def _fail_admission_write_once(monkeypatch, gateway, write_number):
    original = gateway.db.set_native_session_submit_admission
    writes = []

    def write(**kwargs):
        writes.append(kwargs)
        if len(writes) == write_number:
            raise RuntimeError("admission write failed once")
        return original(**kwargs)

    monkeypatch.setattr(gateway.db, "set_native_session_submit_admission", write)
    return writes


@pytest.mark.asyncio
async def test_idle_retry_after_admission_write_failure_runs_once(internal_gateway, monkeypatch):
    gateway = internal_gateway
    await _bind_internal_gateway(gateway)
    # The start hook writes first, then the HTTP route writes its receipt.
    writes = _fail_admission_write_once(monkeypatch, gateway, 2)
    response = await gateway.client.post(
        f"/api/sessions/{SESSION_ID}/submit", headers={"Authorization": "Bearer local-api-key"},
        json=_request("idle-write-failure", "one idle message"))
    await response.read()
    status, receipt = await _submit_internal_gateway(gateway, "idle-write-failure", "one idle message")
    assert status == 202, receipt
    rows = await _wait_native_terminal(gateway, receipt["native_request_ref"])
    await _wait_gateway_idle(gateway)
    assert len(gateway.requests) == 1
    assert [(row["role"], row["content"]) for row in rows] == [
        ("user", "one idle message"), ("assistant", "reply-1")]
    assert receipt["native_request_ref"] == writes[0]["native_request_ref"]
    assert response.status == 503
    assert json.loads(await response.text())["error"]["code"] == "native_admission_unavailable"


@pytest.mark.asyncio
async def test_queued_retry_after_admission_write_failure_runs_once(internal_gateway, monkeypatch):
    gateway = internal_gateway
    key = await _bind_internal_gateway(gateway)
    gateway.release.clear()
    status, first = await _submit_internal_gateway(gateway, "running-before-retry", "first message")
    assert status == 202, first
    await asyncio.wait_for(gateway.started.wait(), timeout=10)
    writes = _fail_admission_write_once(monkeypatch, gateway, 1)
    response = await gateway.client.post(
        f"/api/sessions/{SESSION_ID}/submit", headers={"Authorization": "Bearer local-api-key"},
        json=_request("queued-write-failure", "one queued message"))
    await response.read()
    status, receipt = await _submit_internal_gateway(gateway, "queued-write-failure", "one queued message")
    assert status == 202 and receipt["admission"] == "queued", receipt
    assert receipt["native_request_ref"] == writes[0]["native_request_ref"]
    gateway.release.set()
    await _wait_native_terminal(gateway, receipt["native_request_ref"])
    rows = await _wait_gateway_idle(gateway)
    assert len(gateway.requests) == 2
    assert [(row["role"], row["content"]) for row in rows] == [
        ("user", "first message"), ("assistant", "reply-1"),
        ("user", "one queued message"), ("assistant", "reply-2")]
    assert key not in gateway.adapter._pending_messages
    assert response.status == 503
    assert json.loads(await response.text())["error"]["code"] == "native_admission_unavailable"


@pytest.mark.asyncio
async def test_start_timeout_retry_reuses_pending_start_and_runs_once(internal_gateway, monkeypatch):
    gateway = internal_gateway
    await _bind_internal_gateway(gateway)
    start_entered, release_start = asyncio.Event(), asyncio.Event()
    refs = []
    original_start = gateway.adapter._on_native_submit_started
    gateway.adapter._native_submit_start_timeout_s = 0.02

    async def delayed_start(event, session_key):
        refs.append(event.metadata["native_request_ref"])
        start_entered.set()
        await release_start.wait()
        return await original_start(event, session_key)

    monkeypatch.setattr(gateway.adapter, "_on_native_submit_started", delayed_start)
    status, body = await _submit_internal_gateway(gateway, "start-timeout", "one delayed message")
    assert status == 503 and body["error"]["code"] == "native_admission_unavailable"
    await asyncio.wait_for(start_entered.wait(), timeout=10)
    first_ref = refs[0]
    # Retry before the delayed hook completes, so it must reuse the live future.
    retry_registered = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_register = gateway.db.register_native_session_submit

    def register(*args, **kwargs):
        result = original_register(*args, **kwargs)
        loop.call_soon_threadsafe(retry_registered.set)
        return result

    monkeypatch.setattr(gateway.db, "register_native_session_submit", register)
    gateway.adapter._native_submit_start_timeout_s = 1.0
    retry = asyncio.create_task(_submit_internal_gateway(gateway, "start-timeout", "one delayed message"))
    try:
        await asyncio.wait_for(retry_registered.wait(), timeout=10)
    finally:
        release_start.set()
    status, receipt = await asyncio.wait_for(retry, timeout=10)
    assert status == 202, receipt
    await _wait_native_terminal(gateway, first_ref)
    rows = await _wait_gateway_idle(gateway)
    assert len(gateway.requests) == 1
    assert len(rows) == 2
    assert receipt["native_request_ref"] == first_ref
    assert refs == [first_ref]


@pytest.mark.asyncio
async def test_start_never_fires_retry_releases_session_lock(internal_gateway, monkeypatch):
    gateway = internal_gateway
    await _bind_internal_gateway(gateway)
    gateway.adapter._native_submit_start_timeout_s = 0.02
    entered, release_start = asyncio.Event(), asyncio.Event()
    refs = []
    original_start = gateway.adapter._on_native_submit_started

    async def delayed_start(event, session_key):
        refs.append(event.metadata["native_request_ref"])
        entered.set()
        await release_start.wait()
        await original_start(event, session_key)

    monkeypatch.setattr(gateway.adapter, "_on_native_submit_started", delayed_start)
    try:
        status, body = await _submit_internal_gateway(gateway, "never-starts", "held start")
        assert status == 503 and body["error"]["code"] == "native_admission_unavailable"
        await asyncio.wait_for(entered.wait(), 1)
        ref = refs[0]
        retry = asyncio.create_task(_submit_internal_gateway(gateway, "never-starts", "held start"))
        other = asyncio.create_task(_submit_internal_gateway(gateway, "other-submit", "other message"))
        status, body = await asyncio.wait_for(retry, 1)
        assert status == 503 and body["error"]["code"] == "native_admission_unavailable"
        status, receipt = await asyncio.wait_for(other, 1)
        assert status == 202 and receipt["admission"] == "queued", receipt
        pending = gateway.db.register_native_session_submit(
            SESSION_ID, external_request_id="never-starts",
            message_sha256=gateway.adapter._native_submit_fingerprint("held start", "queue"),
            native_request_ref="unused")
        assert pending["native_request_ref"] == ref and pending["admission"] is None
        assert not gateway.adapter._native_submit_started[ref].cancelled()
        assert refs == [ref]
    finally:
        release_start.set()
    await _wait_native_terminal(gateway, receipt["native_request_ref"])
    rows = await _wait_gateway_idle(gateway)
    assert len(gateway.requests) == 2
    assert len(rows) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_before_retry", [False, True])
async def test_start_hook_write_failure_retry_settles_once(internal_gateway, monkeypatch, finish_before_retry):
    gateway = internal_gateway
    key = await _bind_internal_gateway(gateway)
    gateway.release.clear()
    writes = _fail_admission_write_once(monkeypatch, gateway, 1)
    status, body = await _submit_internal_gateway(gateway, "hook-write-failure", "one hook message")
    assert status == 503 and body["error"]["code"] == "native_admission_unavailable"
    ref = writes[0]["native_request_ref"]
    await asyncio.wait_for(gateway.started.wait(), 10)
    if finish_before_retry:
        gateway.release.set()
        await _wait_native_terminal(gateway, ref)
        await _wait_gateway_idle(gateway)
    else:
        assert gateway.adapter._native_submit_active_ref(key) == ref
    status, receipt = await _submit_internal_gateway(gateway, "hook-write-failure", "one hook message")
    assert status == 202 and receipt["admission"] == "streaming", receipt
    assert receipt["native_request_ref"] == ref
    persisted = gateway.db.register_native_session_submit(
        SESSION_ID, external_request_id="hook-write-failure",
        message_sha256=gateway.adapter._native_submit_fingerprint("one hook message", "queue"),
        native_request_ref="unused")
    assert persisted["admission"] == "streaming"
    gateway.release.set()
    await _wait_native_terminal(gateway, ref)
    rows = await _wait_gateway_idle(gateway)
    assert len(gateway.requests) == 1
    assert [(row["role"], row["content"]) for row in rows] == [
        ("user", "one hook message"), ("assistant", "reply-1")]


@pytest.mark.asyncio
async def test_internal_submit_without_bound_credential_keeps_409(internal_gateway):
    status, body = await _submit_internal_gateway(internal_gateway, "internal-unbound", internal=True)
    assert status == 409 and body["error"]["code"] == "credential_unavailable"
    assert internal_gateway.requests == []
    assert internal_gateway.db.get_messages(SESSION_ID) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("internal", ["yes", "false", 0, 1, None, [], {}])
async def test_internal_submit_rejects_non_boolean_schema_values(internal_gateway, internal):
    status, body = await _submit_internal_gateway(internal_gateway, "internal-bad-schema", internal=internal)
    assert status == 400 and body["error"]["code"] == "invalid_native_submit_schema"


@pytest.mark.asyncio
async def test_internal_false_is_an_ordinary_turn(internal_gateway):
    gateway = internal_gateway
    await _bind_internal_gateway(gateway)
    status, receipt = await _submit_internal_gateway(gateway, "explicit-user", internal=False)
    assert status == 202, receipt
    rows = await _wait_native_terminal(gateway, receipt["native_request_ref"])
    assert rows[0]["role"] == "user" and rows[0].get("display_kind") is None


@pytest.mark.asyncio
async def test_internal_gateway_harness_runs_an_ordinary_turn(internal_gateway):
    await _bind_internal_gateway(internal_gateway)
    status, receipt = await _submit_internal_gateway(internal_gateway, "harness-user")
    assert status == 202, receipt
    rows = await _wait_native_terminal(internal_gateway, receipt["native_request_ref"])
    assert [(row["role"], row["content"]) for row in rows] == [("user", "Work result"), ("assistant", "reply-1")]
    assert len(internal_gateway.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("first,second", [(False, True), (True, False)])
async def test_internal_flag_change_conflicts_instead_of_replaying(internal_gateway, first, second):
    gateway = internal_gateway
    await _bind_internal_gateway(gateway)
    status, receipt = await _submit_internal_gateway(gateway, "changed-internal", internal=first)
    assert status == 202, receipt
    await _wait_native_terminal(gateway, receipt["native_request_ref"])
    status, body = await _submit_internal_gateway(gateway, "changed-internal", internal=second)
    assert status == 409 and body["error"]["code"] == "native_submit_idempotency_conflict"
    assert len(gateway.requests) == 1
