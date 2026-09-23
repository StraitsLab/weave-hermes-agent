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


# ── catch-up endpoint ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_events_after_returns_exactly_later_rows_with_messages_fields(adapter):
    db = adapter._session_db
    ids = _seed(db, 5)
    client = await _client(adapter)
    try:
        resp = await client.get(f"/api/sessions/{SID}/events?after={ids[1]}", headers=AUTH)
        assert resp.status == 200
        body = await resp.json()
        msgs = await (await client.get(
            f"/api/sessions/{SID}/messages?limit=500&order=oldest", headers=AUTH
        )).json()
    finally:
        await client.close()

    assert body["object"] == "conversation_events"
    assert body["session_id"] == SID
    assert body["reset_required"] is False
    assert body["head"] == ids[-1]
    assert [item["cursor"] for item in body["items"]] == ids[2:]
    by_id = {m["id"]: m for m in msgs["data"]}
    for item in body["items"]:
        assert item["message"] == by_id[item["cursor"]]


@pytest.mark.asyncio
async def test_events_validation_and_auth(adapter):
    client = await _client(adapter)
    try:
        assert (await client.get(f"/api/sessions/{SID}/events")).status == 401
        for query in ("after=-1", "after=x", "limit=0", "limit=501"):
            resp = await client.get(f"/api/sessions/{SID}/events?{query}", headers=AUTH)
            assert resp.status == 400, query
        assert (await client.get("/api/sessions/nope/events", headers=AUTH)).status == 404
        body = await (await client.get(f"/api/sessions/{SID}/events", headers=AUTH)).json()
        assert body == {
            "object": "conversation_events", "session_id": SID, "head": 0,
            "reset_required": False, "items": [],
        }
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_events_limit_pages_ascending(adapter):
    ids = _seed(adapter._session_db, 5)
    client = await _client(adapter)
    try:
        body = await (await client.get(
            f"/api/sessions/{SID}/events?after=0&limit=2", headers=AUTH
        )).json()
    finally:
        await client.close()
    assert [i["cursor"] for i in body["items"]] == ids[:2]


@pytest.mark.asyncio
async def test_rewind_after_cursor_requires_reset(adapter):
    db = adapter._session_db
    ids = _seed(db, 4)
    db.rewind_to_message(SID, ids[2])  # ids[2], ids[3] become inactive
    client = await _client(adapter)
    try:
        body = await (await client.get(
            f"/api/sessions/{SID}/events?after={ids[3]}", headers=AUTH
        )).json()
    finally:
        await client.close()
    assert body["reset_required"] is True
    assert [i["cursor"] for i in body["items"]] == ids[:2]
    assert body["head"] == ids[1]


@pytest.mark.asyncio
async def test_compaction_requires_reset(adapter):
    db = adapter._session_db
    ids = _seed(db, 4)
    db.archive_and_compact(SID, [{"role": "user", "content": "summary"}])
    client = await _client(adapter)
    try:
        body = await (await client.get(
            f"/api/sessions/{SID}/events?after={ids[-1]}", headers=AUTH
        )).json()
    finally:
        await client.close()
    assert body["reset_required"] is True
    assert [i["message"]["content"] for i in body["items"]] == ["summary"]
    assert body["items"][0]["cursor"] > ids[-1]


@pytest.mark.asyncio
async def test_compression_fork_changes_resolved_session_and_requires_reset(adapter):
    db = adapter._session_db
    ids = _seed(db, 2)
    db.publish_compression_child(
        parent_session_id=SID,
        child_session_id="event-log-child",
        source="api_server",
        messages=[{"role": "user", "content": "handoff"}],
        require_compression_lease=False,
    )
    client = await _client(adapter)
    try:
        body = await (await client.get(
            f"/api/sessions/{SID}/events?after={ids[-1]}", headers=AUTH
        )).json()
    finally:
        await client.close()
    assert body["session_id"] == "event-log-child"
    assert body["reset_required"] is True
    assert [i["message"]["content"] for i in body["items"]] == ["handoff"]


def test_routes_and_capabilities_advertise_event_log(adapter):
    paths = {p for _m, p, _h in adapter._http_route_table()}
    assert "/api/sessions/{session_id}/events" in paths
    assert "/api/sessions/{session_id}/events/stream" in paths


@pytest.mark.asyncio
async def test_capabilities_list_event_endpoints(adapter):
    client = await _client(adapter)
    try:
        body = await (await client.get("/v1/capabilities", headers=AUTH)).json()
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
    client = await _client(adapter)
    try:
        resp = await client.get(
            f"/api/sessions/{SID}/events/stream",
            headers={**AUTH, "Last-Event-ID": str(ids[1])},
        )
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/event-stream")
        replay = [await _read_frame(resp), await _read_frame(resp)]
        assert [f["event"] for f in replay] == ["item", "item"]
        assert [int(f["id"]) for f in replay] == ids[2:]
        assert [f["data"]["cursor"] for f in replay] == ids[2:]

        new_id = await _append_from_thread(db, "live")
        frame = await _read_frame(resp, timeout=1.0)
        assert frame["event"] == "item"
        assert int(frame["id"]) == new_id
        assert frame["data"]["message"]["content"] == "live"
        resp.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stream_query_after_and_unknown_cursor_replays_reset(adapter):
    db = adapter._session_db
    ids = _seed(db, 2)
    client = await _client(adapter)
    try:
        resp = await client.get(
            f"/api/sessions/{SID}/events/stream?after={ids[-1] + 100}", headers=AUTH
        )
        first = await _read_frame(resp)
        assert first["event"] == "reset"
        assert first["data"] == {"head": ids[-1]}
        items = [await _read_frame(resp), await _read_frame(resp)]
        assert [int(f["id"]) for f in items] == ids
        resp.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stream_rewind_while_connected_emits_reset(adapter):
    db = adapter._session_db
    ids = _seed(db, 4)
    client = await _client(adapter)
    try:
        resp = await client.get(
            f"/api/sessions/{SID}/events/stream",
            headers={**AUTH, "Last-Event-ID": str(ids[-2])},
        )
        assert resp.status == 200
        # Replay done: the handler is parked on live notes.
        assert int((await _read_frame(resp))["id"]) == ids[-1]
        await asyncio.to_thread(db.rewind_to_message, SID, ids[2])
        frame = await _read_frame(resp, timeout=1.0)
        assert frame["event"] == "reset"
        assert frame["data"] == {"head": ids[1]}
        replay = [await _read_frame(resp), await _read_frame(resp)]
        assert [int(f["id"]) for f in replay] == ids[:2]
        resp.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stream_disconnect_unsubscribes(adapter):
    db = adapter._session_db
    _seed(db, 1)
    client = await _client(adapter)
    try:
        resp = await client.get(f"/api/sessions/{SID}/events/stream", headers=AUTH)
        await _read_frame(resp)
        assert db.commit_listener_count() == 1
        resp.close()
        for _ in range(200):
            if db.commit_listener_count() == 0:
                break
            await asyncio.sleep(0.01)
        assert db.commit_listener_count() == 0
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
    real_get_messages = db.get_messages

    def spy(*args, **kwargs):
        calls["n"] += 1
        return real_get_messages(*args, **kwargs)

    monkeypatch.setattr(db, "get_messages", spy)
    client = await _client(adapter)
    try:
        resp = await client.get(f"/api/sessions/{SID}/events/stream", headers=AUTH)
        assert (await _read_frame(resp))["event"] == "item"
        baseline = calls["n"]
        frames = [await _read_frame(resp, timeout=interval * 5) for _ in range(3)]
        assert frames == [{"comment": ": keepalive"}] * 3
        assert calls["n"] == baseline, "idle stream must not read the database"
        resp.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stream_queue_overflow_degrades_to_reset(adapter, monkeypatch):
    db = adapter._session_db
    ids = _seed(db, 2)
    monkeypatch.setattr(api_server_mod, "SESSION_EVENTS_STREAM_QUEUE_MAX", 1)
    client = await _client(adapter)
    try:
        resp = await client.get(
            f"/api/sessions/{SID}/events/stream",
            headers={**AUTH, "Last-Event-ID": str(ids[0])},
        )
        # Replay finished (handler is now parked waiting for notes).
        assert int((await _read_frame(resp))["id"]) == ids[1]
        # Three commits while the event loop is blocked (synchronous calls on
        # the loop thread): the 1-slot queue cannot drain between them.
        for i in range(3):
            db.append_message(SID, role="user", content=f"burst{i}")
        frames = []
        while True:
            frame = await _read_frame(resp, timeout=1.0)
            frames.append(frame)
            if frame.get("event") == "item" and frame["data"]["message"]["content"] == "burst2":
                break
        assert frames[0]["event"] == "reset", [(f.get("event"), f.get("id")) for f in frames]
        assert [int(f["id"]) for f in frames[1:]] == [ids[0], ids[1], ids[1] + 1, ids[1] + 2, ids[1] + 3]
        resp.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stream_follows_compression_fork_to_child(adapter):
    db = adapter._session_db
    ids = _seed(db, 2)
    client = await _client(adapter)
    try:
        resp = await client.get(
            f"/api/sessions/{SID}/events/stream",
            headers={**AUTH, "Last-Event-ID": str(ids[0])},
        )
        # Replay done: the handler is parked on live notes.
        assert int((await _read_frame(resp))["id"]) == ids[1]
        await asyncio.to_thread(
            db.publish_compression_child,
            parent_session_id=SID,
            child_session_id="event-log-live-child",
            source="api_server",
            messages=[{"role": "user", "content": "handoff"}],
            require_compression_lease=False,
        )
        reset = await _read_frame(resp, timeout=1.0)
        assert reset["event"] == "reset"
        handoff = await _read_frame(resp, timeout=1.0)
        assert handoff["data"]["message"]["content"] == "handoff"
        assert handoff["data"]["message"]["session_id"] == "event-log-live-child"

        # A later append to the CHILD (from another thread) is still streamed.
        def append_child():
            return db.append_message("event-log-live-child", role="assistant", content="after fork")

        child_id = await asyncio.to_thread(append_child)
        frame = await _read_frame(resp, timeout=1.0)
        assert frame["event"] == "item" and int(frame["id"]) == child_id
        resp.close()
    finally:
        await client.close()
