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
