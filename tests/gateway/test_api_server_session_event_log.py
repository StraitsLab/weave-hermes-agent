"""Committed-transcript event log: catch-up endpoint + resumable SSE (WEV-1817)."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import gateway.platforms.api_server as api_server_mod
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB


SID = "event-log-session"
KEY = "sk-event-log-test"
AUTH = {"Authorization": f"Bearer {KEY}"}


@pytest.fixture
def adapter(tmp_path):
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": KEY}))
    db = SessionDB(tmp_path / "state.db")
    db.create_session(SID, "api_server")
    adapter._session_db = db
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


def _seed(db, n):
    ids = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        ids.append(db.append_message(SID, role=role, content=f"m{i}"))
    return ids


async def _read_frame(response, timeout=2.0):
    raw = await asyncio.wait_for(response.content.readuntil(b"\n\n"), timeout)
    if not raw:
        return {"eof": True}
    text = raw.decode()
    if text.startswith(":"):
        return {"comment": text.strip()}
    frame = {}
    for line in text.strip().split("\n"):
        field, _, value = line.partition(": ")
        frame[field] = value
    if "data" in frame:
        frame["data"] = json.loads(frame["data"])
    return frame


async def _append_from_thread(db, content):
    result = {}

    def work():
        result["thread"] = threading.get_ident()
        result["id"] = db.append_message(SID, role="user", content=content)

    t = threading.Thread(target=work)
    t.start()
    await asyncio.to_thread(t.join, 5)
    assert result["thread"] != threading.get_ident()
    return result["id"]


def _c(epoch, message_id):
    return f"{epoch}.{message_id}"


async def _get(client, path):
    resp = await client.get(path, headers=AUTH)
    assert resp.status == 200, (path, resp.status)
    return await resp.json()


async def _stream(client, cursor=None, sid=SID):
    headers = dict(AUTH)
    if cursor is not None:
        headers["Last-Event-ID"] = cursor
    resp = await client.get(f"/api/sessions/{sid}/events/stream", headers=headers)
    assert resp.status == 200
    return resp


async def _eof(response, timeout=2.0):
    return await asyncio.wait_for(response.content.read(), timeout) == b""


# ── catch-up endpoint ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_events_after_returns_exactly_later_rows_with_messages_fields(adapter):
    db = adapter._session_db
    ids = _seed(db, 5)
    epoch = db.get_transcript_epoch(SID)
    client = await _client(adapter)
    try:
        body = await _get(client, f"/api/sessions/{SID}/events?after={_c(epoch, ids[1])}")
        msgs = await _get(client, f"/api/sessions/{SID}/messages?limit=500&order=oldest")
    finally:
        await client.close()
    assert body["object"] == "conversation_events"
    assert body["session_id"] == SID and body["epoch"] == epoch
    assert body["reset_required"] is False
    assert body["head"] == _c(epoch, ids[-1])
    assert [i["cursor"] for i in body["items"]] == [_c(epoch, i) for i in ids[2:]]
    by_id = {m["id"]: m for m in msgs["data"]}
    for item in body["items"]:
        assert item["message"] == by_id[item["message"]["id"]]


@pytest.mark.asyncio
async def test_events_validation_and_auth(adapter):
    client = await _client(adapter)
    try:
        assert (await client.get(f"/api/sessions/{SID}/events")).status == 401
        for query in ("after=-1", "after=x", "after=5", "after=1.x", "limit=0", "limit=501"):
            resp = await client.get(f"/api/sessions/{SID}/events?{query}", headers=AUTH)
            assert resp.status == 400, query
        assert (await client.get("/api/sessions/nope/events", headers=AUTH)).status == 404
        body = await _get(client, f"/api/sessions/{SID}/events")
        epoch = body["epoch"]  # a session starts at a fresh, never-reissued epoch
        assert isinstance(epoch, int) and epoch > 0
        assert body == {"object": "conversation_events", "session_id": SID, "epoch": epoch,
                        "head": f"{epoch}.0", "reset_required": True, "items": []}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_events_limit_pages_ascending(adapter):
    ids = _seed(adapter._session_db, 5)
    client = await _client(adapter)
    try:
        body = await _get(client, f"/api/sessions/{SID}/events?after=0&limit=2")
    finally:
        await client.close()
    assert [i["message"]["id"] for i in body["items"]] == ids[:2]


@pytest.mark.parametrize("change", [
    "rewind", "restore", "compaction", "repair", "display_kind", "fork", "clear",
])
@pytest.mark.asyncio
async def test_every_non_append_change_resets_a_disconnected_client(adapter, change):
    """The client read up to its cursor, disconnected, the change committed,
    and it reconnects: it must be told to re-read (review findings 2, 3, 6)."""
    db = adapter._session_db
    blank = db.append_message(SID, role="assistant", content="")
    ids = [blank] + _seed(db, 3)
    db.append_message(SID, role="user", content="synthetic prompt")
    epoch = db.get_transcript_epoch(SID)
    tip = db.get_active_message_watermark(SID)
    if change in ("restore",):
        db.rewind_to_message(SID, ids[3])
        epoch = db.get_transcript_epoch(SID)
        tip = db.get_active_message_watermark(SID)
    cursor = _c(epoch, tip)
    {
        "rewind": lambda: db.rewind_to_message(SID, ids[3]),
        "restore": lambda: db.restore_rewound(SID, ids[3]),
        "compaction": lambda: db.archive_and_compact(SID, [{"role": "user", "content": "summary"}]),
        "repair": lambda: db.append_messages_batch(
            SID, [{"role": "assistant", "content": "final answer", "_row_id": blank}]),
        "display_kind": lambda: db.set_latest_matching_message_display_kind(
            SID, role="user", content="synthetic prompt", display_kind="internal"),
        "fork": lambda: db.publish_compression_child(
            parent_session_id=SID, child_session_id="event-log-child", source="api_server",
            messages=[{"role": "user", "content": "handoff"}], require_compression_lease=False),
        "clear": lambda: db.clear_messages(SID),
    }[change]()
    client = await _client(adapter)
    try:
        page = await _get(client, f"/api/sessions/{SID}/events?after={cursor}")
        msgs = await _get(client, f"/api/sessions/{SID}/messages?limit=500&order=oldest")
        resp = await _stream(client, cursor)
        first = await _read_frame(resp)
        assert await _eof(resp), "a reset ends the stream"
    finally:
        await client.close()
    assert page["reset_required"] is True
    assert page["epoch"] > epoch
    resolved = db.resolve_resume_session_id(SID)
    assert page["session_id"] == resolved
    if resolved == SID:
        assert [i["message"] for i in page["items"]] == msgs["data"]
    assert first == {"event": "reset", "data": {"cursor": _c(page["epoch"], 0)}}


@pytest.mark.asyncio
async def test_sibling_append_moves_resolution_and_resets(adapter):
    """Review finding 7: an append to an older sibling makes it the tip."""
    db = adapter._session_db
    db.append_message(SID, role="user", content="root")
    for sid in ("a", "b"):
        db.create_session(sid, "api_server", parent_session_id=SID)
        db.append_message(sid, role="user", content=sid)
    db.end_session(SID, "compression")
    assert db.resolve_resume_session_id(SID) == "b"
    first = await asyncio.to_thread(db.read_transcript_events, SID, None, 200)
    cursor = (first["epoch"], first["head"])
    db.append_message("a", role="assistant", content="newly active sibling")
    assert db.resolve_resume_session_id(SID) == "a"
    page = await asyncio.to_thread(db.read_transcript_events, SID, cursor, 200)
    assert page["session_id"] == "a" and page["reset_required"] is True


@pytest.mark.asyncio
async def test_sibling_heartbeat_moves_resolution_and_resets(adapter):
    db = adapter._session_db
    db.append_message(SID, role="user", content="root")
    for sid in ("a", "b"):
        db.create_session(sid, "api_server", parent_session_id=SID)
        db.append_message(sid, role="user", content=sid)
    db.end_session(SID, "compression")
    first = await asyncio.to_thread(db.read_transcript_events, SID, None, 200)
    assert first["session_id"] == "b"
    db.touch_session_activity("a")
    assert db.resolve_resume_session_id(SID) == "a"
    page = await asyncio.to_thread(db.read_transcript_events, SID, (first["epoch"], first["head"]), 200)
    assert page["session_id"] == "a" and page["reset_required"] is True


@pytest.mark.asyncio
async def test_ending_the_tip_sibling_and_deleting_an_empty_tip_reset(adapter):
    db = adapter._session_db
    db.append_message(SID, role="user", content="root")
    db.create_session("a", "api_server", parent_session_id=SID)
    db.append_message("a", role="user", content="a message")
    db.create_session("b", "api_server", parent_session_id=SID)
    db.append_message("b", role="user", content="b message")
    db.end_session(SID, "compression")
    before = await asyncio.to_thread(db.read_transcript_events, SID, None, 200)
    assert before["session_id"] == "b"
    db.end_session("b", "agent_close")
    page = await asyncio.to_thread(db.read_transcript_events, SID, (before["epoch"], before["head"]), 200)
    assert page["reset_required"] is True
    assert page["session_id"] == db.resolve_resume_session_id(SID)


@pytest.mark.asyncio
async def test_empty_continuation_taking_over_the_resume_resets(adapter):
    """A new, still-empty compression continuation becomes the resolved tip."""
    db = adapter._session_db
    db.append_message(SID, role="user", content="root history")
    db.end_session(SID, "compression")
    before = await asyncio.to_thread(db.read_transcript_events, SID, None, 200)
    assert before["session_id"] == SID
    db.create_session("empty-tip", "api_server", parent_session_id=SID)
    assert db.resolve_resume_session_id(SID) == "empty-tip"
    page = await asyncio.to_thread(db.read_transcript_events, SID, (before["epoch"], before["head"]), 200)
    assert page["session_id"] == "empty-tip" and page["reset_required"] is True and page["rows"] == []


@pytest.mark.asyncio
async def test_empty_tip_delete_resets(adapter):
    db = adapter._session_db
    db.append_message(SID, role="user", content="root history")
    db.end_session(SID, "compression")
    db.create_session("empty-tip", "api_server", parent_session_id=SID)
    assert db.resolve_resume_session_id(SID) == "empty-tip"
    before = await asyncio.to_thread(db.read_transcript_events, SID, None, 200)
    assert db.delete_session_if_empty("empty-tip")
    page = await asyncio.to_thread(db.read_transcript_events, SID, (before["epoch"], before["head"]), 200)
    assert page["session_id"] == SID and page["reset_required"] is True
    assert [r["content"] for r in page["rows"]] == ["root history"]


@pytest.mark.asyncio
async def test_import_of_a_child_resets(adapter):
    db = adapter._session_db
    db.append_message(SID, role="user", content="root message")
    before = await asyncio.to_thread(db.read_transcript_events, SID, None, 200)
    assert db.import_sessions([{"id": "imported", "source": "api_server", "parent_session_id": SID,
                                "messages": [{"role": "user", "content": "imported continuation"}]}])["ok"]
    page = await asyncio.to_thread(db.read_transcript_events, SID, (before["epoch"], before["head"]), 200)
    assert page["session_id"] == "imported" and page["reset_required"] is True


@pytest.mark.asyncio
async def test_page_is_one_consistent_snapshot_under_a_concurrent_compaction(adapter, tmp_path):
    """Review finding 4: a compaction between the validity check and the row
    read must not produce a page that mixes the old cursor with new rows."""
    db = adapter._session_db
    ids = _seed(db, 2)
    epoch = db.get_transcript_epoch(SID)
    other = SessionDB(tmp_path / "state.db")
    real = db.resolve_resume_session_id
    fired = {"n": 0}

    def resolve_then_compact(sid):
        answer = real(sid)
        if not fired["n"]:
            fired["n"] = 1
            other.archive_and_compact(SID, [{"role": "user", "content": "summary"}])
        return answer

    db.resolve_resume_session_id = resolve_then_compact
    try:
        page = await asyncio.to_thread(db.read_transcript_events, SID, (epoch, ids[-1]), 200)
    finally:
        del db.resolve_resume_session_id
        other.close()
    assert fired["n"] == 1
    assert page["reset_required"] is True and page["epoch"] > epoch
    assert [r["content"] for r in page["rows"]] == ["summary"]


def test_routes_and_capabilities_advertise_event_log(adapter):
    paths = {p for _m, p, _h in adapter._http_route_table()}
    assert "/api/sessions/{session_id}/events" in paths
    assert "/api/sessions/{session_id}/events/stream" in paths


@pytest.mark.asyncio
async def test_capabilities_list_event_endpoints(adapter):
    client = await _client(adapter)
    try:
        body = await _get(client, "/v1/capabilities")
    finally:
        await client.close()
    endpoints = body["endpoints"]
    assert endpoints["session_events"]["path"] == "/api/sessions/{session_id}/events"
    assert endpoints["session_events_stream"]["path"] == "/api/sessions/{session_id}/events/stream"


# ── SSE stream ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_resumes_from_last_event_id_then_delivers_live_append(adapter):
    db = adapter._session_db
    ids = _seed(db, 4)
    epoch = db.get_transcript_epoch(SID)
    client = await _client(adapter)
    try:
        resp = await _stream(client, _c(epoch, ids[1]))
        assert resp.headers["Content-Type"].startswith("text/event-stream")
        replay = [await _read_frame(resp), await _read_frame(resp)]
        assert [f["event"] for f in replay] == ["item", "item"]
        assert [f["id"] for f in replay] == [_c(epoch, i) for i in ids[2:]]
        assert [f["data"]["cursor"] for f in replay] == [f["id"] for f in replay]
        new_id = await _append_from_thread(db, "live")
        frame = await _read_frame(resp, timeout=1.0)
        assert frame["event"] == "item" and frame["id"] == _c(epoch, new_id)
        assert frame["data"]["message"]["content"] == "live"
        resp.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stream_without_cursor_replays_from_start_without_reset(adapter):
    db = adapter._session_db
    ids = _seed(db, 2)
    epoch = db.get_transcript_epoch(SID)
    client = await _client(adapter)
    try:
        resp = await _stream(client)
        frames = [await _read_frame(resp), await _read_frame(resp)]
        assert [f["id"] for f in frames] == [_c(epoch, i) for i in ids]
        resp.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stream_stale_epoch_sends_reset_then_ends_and_reconnect_replays(adapter):
    db = adapter._session_db
    ids = _seed(db, 3)
    stale = _c(db.get_transcript_epoch(SID), ids[-1])
    db.rewind_to_message(SID, ids[2])
    epoch = db.get_transcript_epoch(SID)
    client = await _client(adapter)
    try:
        resp = await _stream(client, stale)
        assert await _read_frame(resp) == {"event": "reset", "data": {"cursor": _c(epoch, 0)}}
        assert await _eof(resp)
        again = await _stream(client, _c(epoch, 0))
        frame = await _read_frame(again)
        assert frame["id"] == _c(epoch, ids[0])
        again.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stream_reset_while_connected_ends_the_stream_and_cursors_only_increase(adapter):
    """Review finding 5: after a reset the same connection must not replay
    lower cursors; it ends, and the reconnect starts the new epoch."""
    db = adapter._session_db
    ids = _seed(db, 4)
    epoch = db.get_transcript_epoch(SID)
    client = await _client(adapter)
    try:
        resp = await _stream(client)
        frames = [await _read_frame(resp) for _ in ids]
        assert [f["id"] for f in frames] == [_c(epoch, i) for i in ids]
        await asyncio.to_thread(db.rewind_to_message, SID, ids[2])
        new_epoch = db.get_transcript_epoch(SID)
        assert await _read_frame(resp, timeout=1.0) == {"event": "reset", "data": {"cursor": _c(new_epoch, 0)}}
        assert await _eof(resp)
        again = await _stream(client, _c(new_epoch, 0))
        replay = [await _read_frame(again), await _read_frame(again)]
        assert [f["id"] for f in replay] == [_c(new_epoch, i) for i in ids[:2]]
        again.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stream_compaction_while_connected_does_not_hang_the_loop(adapter):
    """Review finding 1: a compaction while connected used to spin the event
    loop forever. The handler must answer and the loop must stay responsive."""
    db = adapter._session_db
    db.append_message(SID, role="user", content="old")
    client = await _client(adapter)
    try:
        resp = await _stream(client)
        assert (await _read_frame(resp))["event"] == "item"
        db.archive_and_compact(SID, [{"role": "user", "content": "summary"}])  # on the loop thread
        assert (await asyncio.wait_for(_read_frame(resp), 2))["event"] == "reset"
        await asyncio.wait_for(asyncio.sleep(0), 1)
        assert await _eof(resp)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stream_follows_a_fork_by_reset_and_reconnect(adapter):
    db = adapter._session_db
    _seed(db, 2)
    client = await _client(adapter)
    try:
        resp = await _stream(client)
        [await _read_frame(resp) for _ in range(2)]
        await asyncio.to_thread(
            db.publish_compression_child, parent_session_id=SID, child_session_id="event-log-live-child",
            source="api_server", messages=[{"role": "user", "content": "handoff"}],
            require_compression_lease=False)
        reset = await _read_frame(resp, timeout=1.0)
        assert reset["event"] == "reset" and await _eof(resp)
        again = await _stream(client, reset["data"]["cursor"])
        handoff = await _read_frame(again)
        assert handoff["data"]["message"]["content"] == "handoff"
        assert handoff["data"]["message"]["session_id"] == "event-log-live-child"
        child_id = await asyncio.to_thread(
            db.append_message, "event-log-live-child", role="assistant", content="after fork")
        frame = await _read_frame(again, timeout=1.0)
        assert frame["event"] == "item" and frame["id"].endswith(f".{child_id}")
        again.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stream_append_during_replay_is_not_missed(adapter, monkeypatch):
    db = adapter._session_db
    first_id = db.append_message(SID, role="user", content="seed")
    real = db.read_transcript_events
    captured, release = threading.Event(), threading.Event()
    calls = {"n": 0}

    def gated(*args):
        page = real(*args)
        calls["n"] += 1
        if calls["n"] == 1:
            captured.set()
            assert release.wait(5)
        return page

    monkeypatch.setattr(db, "read_transcript_events", gated)
    client = await _client(adapter)
    try:
        resp = await _stream(client)
        assert await asyncio.to_thread(captured.wait, 5)
        new_id = await asyncio.to_thread(db.append_message, SID, role="assistant", content="during replay")
        release.set()
        frames = [await _read_frame(resp), await _read_frame(resp)]
        assert [f["id"].split(".")[1] for f in frames] == [str(first_id), str(new_id)]
        resp.close()
    finally:
        release.set()
        await client.close()


@pytest.mark.asyncio
async def test_stream_disconnect_unsubscribes(adapter):
    db = adapter._session_db
    _seed(db, 1)
    client = await _client(adapter)
    try:
        resp = await _stream(client)
        await _read_frame(resp)
        assert db.commit_listener_count() == 1
        resp.close()
        for _ in range(200):
            if db.commit_listener_count() == 0:
                break
            await asyncio.sleep(0.01)
    finally:
        await client.close()
    assert db.commit_listener_count() == 0


@pytest.mark.asyncio
async def test_stream_idle_sends_only_keepalives_and_never_reads(adapter, monkeypatch):
    db = adapter._session_db
    _seed(db, 1)
    interval = 0.1
    monkeypatch.setattr(api_server_mod, "CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS", interval)
    calls = {"n": 0}
    real = db.get_messages

    def spy(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(db, "get_messages", spy)
    client = await _client(adapter)
    try:
        resp = await _stream(client)
        assert (await _read_frame(resp))["event"] == "item"
        baseline = calls["n"]
        frames = [await _read_frame(resp, timeout=interval * 5) for _ in range(3)]
        assert frames == [{"comment": ": keepalive"}] * 3
        assert calls["n"] == baseline, "idle stream must not read the database"
        resp.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stream_burst_of_appends_coalesces_and_delivers_all_in_order(adapter):
    db = adapter._session_db
    ids = _seed(db, 1)
    epoch = db.get_transcript_epoch(SID)
    client = await _client(adapter)
    try:
        resp = await _stream(client)
        await _read_frame(resp)
        burst = [db.append_message(SID, role="user", content=f"burst{i}") for i in range(50)]  # loop blocked
        frames = [await _read_frame(resp) for _ in burst]
        assert [f["id"] for f in frames] == [_c(epoch, i) for i in burst]
        resp.close()
    finally:
        await client.close()
    assert ids
