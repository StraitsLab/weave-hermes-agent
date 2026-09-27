"""Cron delivery to an api_server (Harso) conversation.

ApiServerAdapter has no push channel: its send() is a stub that always fails.
A cron job born in an api_server session must land its brief in that
session's transcript, where the client reads it (/events, SSE), and report
failure honestly when the append does not land.
"""

import asyncio
import threading
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from cron.scheduler import _deliver_result
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB

SID = "harso-conversation-1"
KEY = "test-key"
AUTH = {"Authorization": f"Bearer {KEY}"}
DIGEST = "1. first link\n2. second link\n3. third\n4. fourth\n5. fifth"


@pytest.fixture
def gateway_loop():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()


@pytest.fixture
def adapter():
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": KEY}))
    adapter._session_db = SessionDB()
    try:
        yield adapter
    finally:
        adapter._session_db.close()


def _seed_scheduling_turn(db):
    db.create_session(SID, "api_server")
    db.append_message(SID, role="user", content="Every morning send me a five-item linked digest")
    db.append_message(SID, role="assistant", content="Scheduled: daily digest at 08:00.")


def _deliver(adapter, loop, wrap=False, sid=SID):
    job = {
        "id": "9cad29fb6620",
        "name": "daily digest",
        "deliver": "origin",
        "origin": {"platform": "api_server", "chat_id": sid},
    }
    cfg = GatewayConfig(platforms={Platform.API_SERVER: PlatformConfig(enabled=True)})
    with patch("gateway.config.load_gateway_config", return_value=cfg), \
         patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": wrap}}):
        return _deliver_result(
            job, DIGEST, adapters={Platform.API_SERVER: adapter}, loop=loop,
        )


def _rows(db, sid=SID):
    return [(m["role"], m["content"]) for m in db.get_messages(sid)]


def test_api_server_job_lands_in_the_session_transcript(adapter, gateway_loop):
    db = adapter._session_db
    _seed_scheduling_turn(db)

    with patch.object(APIServerAdapter, "send", side_effect=AssertionError("send() called")):
        error = _deliver(adapter, gateway_loop)

    assert error is None
    assert _rows(db)[-1] == ("assistant", DIGEST)
    assert len(_rows(db)) == 3
    assert db.try_acquire_session_turn_lease(SID, "next-turn")  # cron released it


def test_wrap_response_matches_what_push_platforms_receive(adapter, gateway_loop):
    db = adapter._session_db
    _seed_scheduling_turn(db)

    assert _deliver(adapter, gateway_loop, wrap=True) is None

    content = _rows(db)[-1][1]
    assert content.startswith("Cronjob Response: daily digest\n(job_id: 9cad29fb6620)")
    assert DIGEST in content


def test_append_that_does_not_land_is_a_delivery_error(adapter, gateway_loop):
    """No session row: the FK rejects the insert, so the run must not say ok."""
    error = _deliver(adapter, gateway_loop, sid="deleted-conversation")

    assert error is not None
    assert "transcript append to api_server:deleted-conversation failed" in error
    assert adapter._session_db.get_messages("deleted-conversation") == []


def test_brief_after_compression_lands_where_the_client_reads(adapter, gateway_loop):
    db = adapter._session_db
    _seed_scheduling_turn(db)
    db.publish_compression_child(
        parent_session_id=SID, child_session_id="harso-child", source="api_server",
        messages=[{"role": "user", "content": "summary"}], require_compression_lease=False,
    )
    assert db.resolve_resume_session_id(SID) == "harso-child"

    assert _deliver(adapter, gateway_loop) is None

    page = db.read_transcript_events(SID, None, 200)
    assert page["rows"][-1]["content"] == DIGEST


async def _client(adapter):
    app = web.Application()
    for method, path, handler in adapter._http_route_table():
        app.router.add_route(method, path, handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def _next_data_frame(response, timeout=5.0):
    while True:
        raw = await asyncio.wait_for(response.content.readuntil(b"\n\n"), timeout)
        text = raw.decode()
        if not text.startswith(":"):
            return text


@pytest.mark.asyncio
async def test_brief_is_visible_on_the_client_event_surface(adapter, gateway_loop):
    """The Harso client reads /events and the SSE stream, not SQLite."""
    db = adapter._session_db
    _seed_scheduling_turn(db)
    client = await _client(adapter)
    try:
        before = await (await client.get(f"/api/sessions/{SID}/events", headers=AUTH)).json()
        stream = await client.get(
            f"/api/sessions/{SID}/events/stream", headers={**AUTH, "Last-Event-ID": before["head"]},
        )
        assert stream.status == 200

        error = await asyncio.to_thread(_deliver, adapter, gateway_loop)
        frame = await _next_data_frame(stream)
        after = await (
            await client.get(f"/api/sessions/{SID}/events?after={before['head']}", headers=AUTH)
        ).json()
    finally:
        await client.close()

    assert error is None
    assert "event: item" in frame and "1. first link" in frame
    assert [(i["message"]["role"], i["message"]["content"]) for i in after["items"]] == [
        ("assistant", DIGEST)
    ]


def test_next_user_turn_builds_a_valid_provider_sequence(adapter, gateway_loop):
    """assistant(scheduling reply) -> assistant(brief) must not reach the provider."""
    from agent.agent_runtime_helpers import repair_message_sequence

    db = adapter._session_db
    _seed_scheduling_turn(db)
    assert _deliver(adapter, gateway_loop) is None

    # The gateway's restore path for the next turn (SessionStore.load_transcript).
    history = db.get_messages_as_conversation(SID, repair_alternation=True)
    messages = history + [{"role": "user", "content": "open item 3"}]
    repair_message_sequence(None, messages)

    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "user"]
    assert "Scheduled: daily digest at 08:00." in messages[1]["content"]
    assert DIGEST in messages[1]["content"]


# --- A brief that fires during a live turn must wait for that turn. ---------
# Without the turn lease, the append lands between a turn's tool results and
# replay repair (SessionStore.load_transcript) drops the later results.


def _start_delivery(adapter, sid, results):
    job = {"id": "digest", "deliver": "origin", "origin": {"platform": "api_server", "chat_id": sid}}
    cfg = GatewayConfig(platforms={Platform.API_SERVER: PlatformConfig(enabled=True)})

    def run():
        with patch("gateway.config.load_gateway_config", return_value=cfg), \
             patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}):
            results.append(_deliver_result(job, DIGEST, adapters={Platform.API_SERVER: adapter}))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(0.5)  # an unfenced append lands now, before the turn's next write
    return thread


def _replayed_tool_ids(db, sid):
    from gateway.session import SessionStore

    store = object.__new__(SessionStore)
    store._db = db
    return [m["tool_call_id"] for m in store.load_transcript(sid) if m["role"] == "tool"]


@pytest.mark.parametrize("mode", ["sequential", "concurrent"])
def test_brief_during_a_live_tool_batch_lands_after_the_turn(mode, adapter):
    from types import SimpleNamespace

    from hermes_constants import get_hermes_home
    from tests.run_agent.test_tool_call_incremental_persistence import (
        _attach_real_session_db, _make_agent, _mock_tool_call,
    )

    sid = f"live-{mode}"
    agent = _make_agent()
    db = _attach_real_session_db(agent, get_hermes_home() / "state.db", sid)
    assert db.try_acquire_session_turn_lease(sid, "live-turn")
    agent._active_session_turn_lease_holder = "live-turn"
    calls = [_mock_tool_call(call_id="c1"), _mock_tool_call(call_id="c2")]
    messages = [
        {"role": "user", "content": "Read both sources"},
        {"role": "assistant", "content": "Reading", "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": "web_search", "arguments": "{}"}}
            for c in calls
        ]},
    ]
    agent._flush_messages_to_session_db(messages)
    results, threads = [], []

    def fire_cron_after_first_result(call_id, *_):
        if call_id == "c1":
            threads.append(_start_delivery(adapter, sid, results))

    agent.tool_complete_callback = fire_cron_after_first_result
    dispatch = (
        patch("run_agent.handle_function_call", return_value="source data")
        if mode == "sequential"
        else patch.object(agent, "_invoke_tool", return_value="source data")
    )
    try:
        with dispatch, patch(
            "agent.tool_executor.maybe_persist_tool_result",
            side_effect=lambda **kw: kw["content"],
        ):
            executor = getattr(agent, f"_execute_tool_calls_{mode}")
            executor(SimpleNamespace(content="", tool_calls=calls), messages, "task")
        assert len(threads) == 1
        assert DIGEST not in [m["content"] for m in db.get_messages(sid)]

        db.release_session_turn_lease(sid, "live-turn")
        threads[0].join(10)

        assert results == [None]
        assert [m["role"] for m in db.get_messages(sid)][-1] == "assistant"
        assert db.get_messages(sid)[-1]["content"] == DIGEST
        assert _replayed_tool_ids(db, sid) == ["c1", "c2"]
    finally:
        db.close()


@pytest.mark.parametrize("codex_interim", [False, True])
@pytest.mark.parametrize("fires", ["before_first_result", "between_results"])
def test_brief_never_splits_a_tool_block(codex_interim, fires, adapter):
    """Codex interim turns (finish_reason=incomplete) lose BOTH results when split."""
    db = adapter._session_db
    sid = f"split-{codex_interim}-{fires}"
    db.create_session(sid, "api_server")
    assert db.try_acquire_session_turn_lease(sid, "live-turn")
    held = {"turn_lease_holder": "live-turn"}
    db.append_message(sid, role="user", content="Read both sources", **held)
    db.append_message(
        sid, role="assistant", content="Reading",
        tool_calls=[{"id": c, "type": "function", "function": {"name": "web_extract", "arguments": "{}"}}
                    for c in ("c1", "c2")],
        finish_reason="incomplete" if codex_interim else None, **held,
    )
    results = []
    thread = _start_delivery(adapter, sid, results) if fires == "before_first_result" else None
    db.append_message(sid, role="tool", tool_call_id="c1", content="ONE", **held)
    thread = thread or _start_delivery(adapter, sid, results)
    db.append_message(sid, role="tool", tool_call_id="c2", content="TWO", **held)
    db.append_message(sid, role="assistant", content="Both read", **held)

    db.release_session_turn_lease(sid, "live-turn")
    thread.join(10)

    assert results == [None]
    assert db.get_messages(sid)[-1]["content"] == DIGEST
    assert _replayed_tool_ids(db, sid) == ["c1", "c2"]


def test_turn_that_outlasts_the_wait_is_a_delivery_error(adapter, gateway_loop):
    db = adapter._session_db
    _seed_scheduling_turn(db)
    assert db.try_acquire_session_turn_lease(SID, "long-turn")

    with patch("cron.scheduler._TRANSCRIPT_LEASE_WAIT_SECONDS", 0.2):
        error = _deliver(adapter, gateway_loop)

    assert error is not None and "conversation busy" in error
    assert len(_rows(db)) == 2
    assert db.try_acquire_session_turn_lease(SID, "long-turn")  # still ours, not stolen
