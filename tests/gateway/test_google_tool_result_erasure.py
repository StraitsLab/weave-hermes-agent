"""Isolated HTTP proof for Google-only tool-result erasure in the serving home."""

from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB


PREFIX = "mcp__google_"
MARKER = "[removed: Google disconnected]"
URL = "/api/erasure/tool-results"
HEADERS = {"Authorization": "Bearer test-key"}


def _seed(home):
    home.mkdir(parents=True, exist_ok=True)
    db = SessionDB(home / "state.db")
    for session in ("one", "two"):
        db.create_session(session, "api_server")
        db.append_message(session, "user", "keep user bytes")
        db.append_message(session, "assistant", "keep assistant bytes", tool_calls=[{
            "id": f"google-{session}", "type": "function",
            "function": {"name": PREFIX + "gmail__read", "arguments": "{}"},
        }])
    db.append_message("one", "tool", "privategmailcanary", tool_name=PREFIX + "gmail__read",
                      tool_call_id="google-one")
    db.append_message("two", "tool", "privatedrivecanary", tool_name=PREFIX + "drive__read",
                      tool_call_id="google-two")
    db.create_session("three", "api_server")
    db.append_message("three", "tool", "keep linear-only bytes", tool_name="mcp__linear__read",
                      tool_call_id="linear-three")
    db.append_message("one", "tool", "keep linear bytes", tool_name="mcp__linear__read",
                      tool_call_id="linear-one")
    # SQL LIKE would treat '_' as a wildcard and erase this unrelated tool.
    db.append_message("two", "tool", "keep lookalike bytes", tool_name="mcpXXgoogleXgmail__read",
                      tool_call_id="lookalike")
    spillover = home / "cache" / "spillover"
    spillover.mkdir(parents=True)
    (spillover / "google-one.txt").write_bytes(b"privategmailcanary")
    (spillover / "linear-one.txt").write_bytes(b"keep linear spillover bytes")
    return db


def _snapshot(db):
    with db._read_ctx() as conn:
        return {
            "sessions": [dict(row) for row in conn.execute("SELECT * FROM sessions ORDER BY id")],
            "messages": [dict(row) for row in conn.execute("SELECT * FROM messages ORDER BY id")],
        }


def _matches(db, text):
    with db._read_ctx() as conn:
        return conn.execute(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?", (text,),
        ).fetchall()


def _assert_erased(before, after):
    affected = {row["session_id"] for row in before["messages"]
                if row["role"] == "tool" and row["tool_name"].startswith(PREFIX)}
    for old, new in zip(before["sessions"], after["sessions"], strict=True):
        if old["id"] in affected:
            assert new["transcript_epoch"] > old["transcript_epoch"]
            assert {k: v for k, v in new.items() if k != "transcript_epoch"} == {
                k: v for k, v in old.items() if k != "transcript_epoch"}
        else:
            assert new == old
    expected_messages = [dict(row) for row in before["messages"]]
    for row in expected_messages:
        if row["role"] == "tool" and row["tool_name"].startswith(PREFIX):
            row["content"] = MARKER
            assert row["api_content"] is None
    assert after["messages"] == expected_messages


def _disk(home):
    return {p.name: p.read_bytes() for p in home.glob("state.db*")}


def _enable(home, value="true"):
    (home / "config.yaml").write_text(f"erasure:\n  google_tool_results_enabled: {value}\n")


def _app(adapter):
    app = web.Application(middlewares=[adapter._make_profile_prefix_middleware()])
    for method, path, handler in adapter._http_route_table():
        app.router.add_route(method, path, handler)
        app.router.add_route(method, "/p/{profile}" + path, handler)
    return app


@pytest.mark.asyncio
async def test_google_erasure_preserves_sessions_other_rows_and_files(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    db = _seed(home)
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-key"}))
    before = _snapshot(db)
    try:
        assert _matches(db, "privategmailcanary")
        assert _matches(db, "privatedrivecanary")
        async with TestClient(TestServer(_app(adapter))) as client:
            # Missing setting defaults off and must not mutate even one byte.
            disk_before = {p.name: p.read_bytes() for p in home.glob("state.db*")}
            assert (await client.post(URL, headers=HEADERS, json={"tool_name_prefix": PREFIX})).status == 404
            assert _snapshot(db) == before
            assert {p.name: p.read_bytes() for p in home.glob("state.db*")} == disk_before
            assert (home / "cache/spillover/google-one.txt").read_bytes() == b"privategmailcanary"

            (home / "config.yaml").write_text("erasure:\n  google_tool_results_enabled: true\n")
            assert (await client.post(URL, json={"tool_name_prefix": PREFIX})).status == 401
            for prefix in ("mcp__linear__", "", "mcp__google", [PREFIX], None):
                rejected = await client.post(URL, headers=HEADERS, json={"tool_name_prefix": prefix})
                assert rejected.status == 400
            assert _snapshot(db) == before

            erased = await client.post(URL, headers=HEADERS, json={"tool_name_prefix": PREFIX})
            assert erased.status == 200
            assert await erased.json() == {"count": 2}
            after = _snapshot(db)
            _assert_erased(before, after)
            assert not _matches(db, "privategmailcanary")
            assert not _matches(db, "privatedrivecanary")
            assert _matches(db, "linear")
            assert not (home / "cache/spillover/google-one.txt").exists()
            assert (home / "cache/spillover/linear-one.txt").read_bytes() == b"keep linear spillover bytes"
            # Secure-delete plus checkpoint must remove raw text from DB/WAL too.
            for path in home.glob("state.db*"):
                raw = path.read_bytes()
                assert b"privategmailcanary" not in raw
                assert b"privatedrivecanary" not in raw
            replay = await client.post(URL, headers=HEADERS, json={"tool_name_prefix": PREFIX})
            assert replay.status == 200
            assert await replay.json() == {"count": 0}
            assert _snapshot(db) == after

            (home / "config.yaml").write_text("erasure:\n  google_tool_results_enabled: false\n")
            assert (await client.post(URL, headers=HEADERS, json={"tool_name_prefix": PREFIX})).status == 404
    finally:
        adapter._close_cached_session_dbs()
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_route", [True, False], ids=["route-first", "root-first"])
async def test_erasure_is_scoped_to_authenticated_route_home(tmp_path, monkeypatch, first_route):
    from agent import secret_scope as ss

    root = tmp_path / "state" / "gateway"
    route = tmp_path / "state" / "profiles" / "general"
    homes = [root, route]
    dbs = [_seed(home) for home in homes]
    monkeypatch.setenv("HERMES_HOME", str(root))
    key = "isolated-general-route-key-123456"
    (route / ".env").write_text(f"API_SERVER_KEY={key}\n")
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-key"}))
    adapter.gateway_runner = SimpleNamespace(config=SimpleNamespace(
        multiplex_profiles=True, multiplex_profile_allowlist=["general"]))
    monkeypatch.setattr("hermes_cli.profiles.profiles_to_serve",
                        lambda **kwargs: [("default", root), ("general", route)])
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda name: route)
    before = [_snapshot(db) for db in dbs]
    urls = [URL, "/p/general" + URL]
    headers = [HEADERS, {"Authorization": f"Bearer {key}"}]
    ss.set_multiplex_active(True)
    try:
        async with TestClient(TestServer(_app(adapter))) as client:
            # Root opt-in must not enable the route home.
            _enable(root)
            disk = [_disk(home) for home in homes]
            assert (await client.post(urls[1], headers=headers[1],
                                      json={"tool_name_prefix": PREFIX})).status == 404
            assert [_disk(home) for home in homes] == disk
            _enable(route)
            _enable(root, "false")
            # Route opt-in must not enable the root home.
            disk = [_disk(home) for home in homes]
            assert (await client.post(URL, headers=HEADERS,
                                      json={"tool_name_prefix": PREFIX})).status == 404
            assert [_disk(home) for home in homes] == disk
            _enable(root)
            # A listener key cannot authorize a named route, or vice versa.
            for i in (0, 1):
                assert (await client.post(urls[i], headers=headers[1 - i],
                                          json={"tool_name_prefix": PREFIX})).status == 401
            assert [_snapshot(db) for db in dbs] == before
            assert (await client.post("/p/unserved" + URL, headers=HEADERS,
                                      json={"tool_name_prefix": PREFIX})).status == 404
            order = (1, 0) if first_route else (0, 1)
            for i in order:
                other = 1 - i
                untouched = _snapshot(dbs[other])
                untouched_disk = _disk(homes[other])
                untouched_spill = (homes[other] / "cache/spillover/google-one.txt")
                spill_before = untouched_spill.read_bytes() if untouched_spill.exists() else None
                response = await client.post(urls[i], headers=headers[i],
                                             json={"tool_name_prefix": PREFIX})
                assert response.status == 200
                assert await response.json() == {"count": 2}
                _assert_erased(before[i], _snapshot(dbs[i]))
                assert _snapshot(dbs[other]) == untouched
                assert _disk(homes[other]) == untouched_disk
                assert (untouched_spill.read_bytes() if untouched_spill.exists() else None) == spill_before
                assert not (homes[i] / "cache/spillover/google-one.txt").exists()
                assert (homes[i] / "cache/spillover/linear-one.txt").read_bytes() == b"keep linear spillover bytes"
                assert not _matches(dbs[i], "privategmailcanary")
                assert not _matches(dbs[i], "privatedrivecanary")
                after = _snapshot(dbs[i])
                replay = await client.post(urls[i], headers=headers[i], json={"tool_name_prefix": PREFIX})
                assert replay.status == 200
                assert await replay.json() == {"count": 0}
                assert _snapshot(dbs[i]) == after
    finally:
        adapter._close_cached_session_dbs()
        for db in dbs:
            db.close()
        ss.set_multiplex_active(False)


@pytest.mark.asyncio
@pytest.mark.parametrize("config", ["", "erasure:\n  google_tool_results_enabled: false\n",
                                    "erasure:\n  google_tool_results_enabled: 'true'\n", "erasure: [\n"],
                         ids=["missing", "off", "wrong-type", "invalid-yaml"])
async def test_disabled_erasure_is_byte_identical(tmp_path, monkeypatch, config):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = _seed(tmp_path)
    (tmp_path / "config.yaml").write_text(config)
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-key"}))
    try:
        before = _snapshot(db)
        disk = _disk(tmp_path)
        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post(URL, headers=HEADERS, json={"tool_name_prefix": PREFIX})
            assert response.status == 404
        assert _disk(tmp_path) == disk
        assert _snapshot(db) == before
        assert (tmp_path / "cache/spillover/google-one.txt").read_bytes() == b"privategmailcanary"
        assert (tmp_path / "cache/spillover/linear-one.txt").read_bytes() == b"keep linear spillover bytes"
    finally:
        adapter._close_cached_session_dbs()
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("escape", ["call-id", "symlink"])
async def test_erasure_refuses_spillover_escape(tmp_path, monkeypatch, escape):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = _seed(tmp_path)
    _enable(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "google-one.txt"
    sentinel.write_bytes(b"keep outside bytes")
    spillover = tmp_path / "cache/spillover"
    if escape == "call-id":
        db.append_message("one", "tool", "private escape text", tool_name=PREFIX + "gmail__read",
                          tool_call_id="../../outside/google-one")
    else:
        for file in spillover.iterdir():
            file.unlink()
        spillover.rmdir()
        spillover.symlink_to(outside, target_is_directory=True)
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-key"}))
    try:
        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post(URL, headers=HEADERS, json={"tool_name_prefix": PREFIX})
            assert response.status == 503
            assert sentinel.read_bytes() == b"keep outside bytes"
    finally:
        adapter._close_cached_session_dbs()
        db.close()


@pytest.mark.asyncio
async def test_erasure_refuses_success_while_wal_checkpoint_is_blocked(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = _seed(tmp_path)
    _enable(tmp_path)
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-key"}))
    reader = sqlite3.connect(tmp_path / "state.db")
    try:
        async with TestClient(TestServer(_app(adapter))) as client:
            # Warm the real per-home cache without an injected database.
            assert (await client.post(URL, headers=HEADERS,
                                      json={"tool_name_prefix": "invalid"})).status == 400
            with adapter._profile_scope(None):
                cached = await adapter._ensure_session_db_async()
            cached._conn.execute("PRAGMA busy_timeout=1")
            reader.execute("BEGIN")
            assert reader.execute("SELECT content FROM messages WHERE tool_call_id='google-one'").fetchone()[0] == "privategmailcanary"
            failed = await client.post(URL, headers=HEADERS, json={"tool_name_prefix": PREFIX})
            assert failed.status == 503
            assert (tmp_path / "cache/spillover/google-one.txt").exists()
            reader.rollback()
            after = _snapshot(db)
            retry = await client.post(URL, headers=HEADERS, json={"tool_name_prefix": PREFIX})
            assert retry.status == 200
            assert await retry.json() == {"count": 0}
            assert _snapshot(db) == after
            assert not (tmp_path / "cache/spillover/google-one.txt").exists()
            assert all(b"privategmailcanary" not in raw for raw in _disk(tmp_path).values())
    finally:
        reader.close()
        adapter._close_cached_session_dbs()
        db.close()


@pytest.mark.asyncio
async def test_erasure_retries_failed_spillover_cleanup(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = _seed(tmp_path)
    _enable(tmp_path)
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-key"}))
    spill = tmp_path / "cache/spillover/google-one.txt"
    unlink = Path.unlink

    def fail_google(path, *args, **kwargs):
        if path == spill:
            raise OSError("isolated cleanup failure")
        return unlink(path, *args, **kwargs)

    try:
        async with TestClient(TestServer(_app(adapter))) as client:
            with monkeypatch.context() as patch:
                patch.setattr(Path, "unlink", fail_google)
                failed = await client.post(URL, headers=HEADERS, json={"tool_name_prefix": PREFIX})
                assert failed.status == 503
                assert spill.exists()
            after = _snapshot(db)
            retry = await client.post(URL, headers=HEADERS, json={"tool_name_prefix": PREFIX})
            assert retry.status == 200
            assert await retry.json() == {"count": 0}
            assert not spill.exists()
            assert _snapshot(db) == after
    finally:
        adapter._close_cached_session_dbs()
        db.close()
