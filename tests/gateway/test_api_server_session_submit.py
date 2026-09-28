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
    adapter.gateway_runner = SimpleNamespace(
        _running=True, session_credential_available=lambda *_args: True,
    )
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

    running = SimpleNamespace(metadata={"native_request_ref": "native-running"})
    await adapter._on_native_submit_finished(running, "native-key")

    assert "native-steered" in adapter._native_submit_terminals
    assert "native-running" in adapter._native_submit_terminals


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
