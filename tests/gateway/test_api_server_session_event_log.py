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
        "restore": lambda: db._write_sql("UPDATE messages SET content=content || ? WHERE session_id=? AND id=?", (" restored", SID, ids[3])),
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
    finally:
        await client.close()
    assert page["reset_required"] is True
    assert page["epoch"] > epoch
    resolved = db.resolve_resume_session_id(SID)
    assert page["session_id"] == resolved
    if resolved == SID:
        # /messages is the display read and also keeps rows in-place compaction archived; the event
        # log holds only active rows, so after a compaction it is the newest suffix of that history.
        assert [i["message"] for i in page["items"]] == msgs["data"][len(msgs["data"]) - len(page["items"]):]


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
    assert "/api/sessions/{session_id}/events/stream" not in paths




# ── SSE stream ───────────────────────────────────────────────────────────






















@pytest.fixture(autouse=True)
def _native_test_store_selection(monkeypatch):
    # v0.22 selects runtime-owned stores before explicit adapter overrides.
    from gateway.platforms import api_server_store
    original = api_server_store.selected_session_db
    def selected(adapter, home):
        return adapter._session_db if adapter._session_db is not None else original(adapter, home)
    monkeypatch.setattr(api_server_store, "selected_session_db", selected)
