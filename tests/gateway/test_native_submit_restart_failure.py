"""A killed Harso submit ends through chat-sync, never an undeliverable resume turn."""
import collections
import queue
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import weave_chat_sync as sync
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore
from hermes_state import SessionDB


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [True, False])
async def test_unclean_boot_fails_native_submits_once_in_their_own_profile(tmp_path, monkeypatch, legacy):
    from hermes_constants import get_hermes_home

    home = tmp_path / "general"
    home.mkdir()
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda profile: home)
    monkeypatch.setattr("hermes_cli.profiles.profiles_to_serve", lambda **kw: [("general", home)])
    monkeypatch.setattr("hermes_constants.get_process_hermes_home", lambda: tmp_path)
    config = GatewayConfig(multiplex_profiles=True)
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
    store._db = None  # Exercise the persisted JSON routing map, as on the reported cell.
    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner.session_store = store
    runner._ledger_crash_left_replies = AsyncMock(return_value=0)
    source = SessionSource(platform=Platform.API_SERVER, chat_id="weave-crashed", user_id="api_server", profile="general")
    entry = store.get_or_create_session(source)
    store.mark_turn_active(entry.session_key)
    other = store.get_or_create_session(SessionSource(platform=Platform.TELEGRAM, chat_id="tg", user_id="u"))
    store.mark_turn_active(other.session_key)
    db = SessionDB(home / "state.db")
    db.create_session(source.chat_id, "api_server")
    for ref in ("finished", "lost", "queued"):
        db.register_native_session_submit(source.chat_id, external_request_id=ref, message_sha256=ref, native_request_ref=ref)
        db.set_native_session_submit_admission(native_request_ref=ref, admission="queued" if ref == "queued" else "streaming")
    db.append_message(source.chat_id, "user", "old question", platform_message_id="finished")
    db.append_message(source.chat_id, "assistant", "old answer")
    db.append_message(source.chat_id, "user", "new question", platform_message_id="lost")
    # Model an existing pre-upgrade table: streaming rows survived normal completion too.
    columns = {r["name"] for r in db._conn.execute("PRAGMA table_info(native_session_submit_idempotency)")}
    if "terminal_event" in columns and legacy:
        db._execute_write(lambda conn: conn.execute("ALTER TABLE native_session_submit_idempotency DROP COLUMN terminal_event"))
    elif not legacy:
        db.set_native_session_submit_terminal("finished", "turn.completed")
        # No routing marker: the durable receipt alone proves this queued submit was lost.
        db.create_session("weave-orphan", "api_server")
        db.register_native_session_submit("weave-orphan", external_request_id="orphan", message_sha256="orphan", native_request_ref="orphan")
        db.set_native_session_submit_admission(native_request_ref="orphan", admission="queued")
    writer = sync._Writer.__new__(sync._Writer)
    writer.events, writer.live = queue.Queue(), {}
    writer.last_at_us, writer.floor_prune_at_us = {}, 0
    monkeypatch.setattr(sync, "_WRITER", writer)
    monkeypatch.setattr(sync, "_ENDED", collections.deque(maxlen=256))
    writes = []
    monkeypatch.setattr(sync, "_call", lambda live, rows: writes.extend((live["session"], r) for r in rows) or rows)
    monkeypatch.setattr("gateway.platforms.api_server_harso_sessions._scoped_get_secret", lambda name, default="": {
        "WEAVE_CHAT_SUPABASE_URL": "https://example.invalid", "WEAVE_CHAT_SUPABASE_KEY": "test", "WEAVE_API_MCP_BEARER": "test",
    }.get(name, default) if get_hermes_home() == home else pytest.fail("wrong profile scope"))
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-key"}))
    adapter.gateway_runner = runner
    adapter._session_db = db
    monkeypatch.setattr(adapter, "_api_key_passes_startup_guard", lambda: True)
    monkeypatch.setattr(adapter, "_wire_plugin_handlers", lambda app: None)
    monkeypatch.setattr(adapter, "_publish_runtime_status", lambda: None)
    monkeypatch.setattr("gateway.platforms.api_server.start_tcp_site", AsyncMock(return_value=SimpleNamespace(stop=AsyncMock())))
    try:
        await runner._recover_unclean_sessions()
        assert await adapter.connect()
        events = []
        while not writer.events.empty():
            ref, event = writer.events.get_nowait()
            events.append((ref, event["type"]))
            writer._take(ref, event)
            writer._write(ref, writer.live[ref])
        expected = [("lost", "turn.failed"), ("queued", "turn.failed")]
        if not legacy:
            expected.append(("orphan", "turn.failed"))
        assert events == expected
        assert {s for s, row in writes} == ({"weave-crashed"} if legacy else {"weave-crashed", "weave-orphan"})
        failures = [row for _, row in writes if row.get("body", {}).get("outcome") == "failed"]
        assert len(failures) == len(expected)
        assert all(row["state"] == "final" and "resend" in row["body"]["text"] for row in failures)
        assert not store._entries[entry.session_key].resume_pending
        assert store._entries[other.session_key].resume_pending
        await adapter.disconnect()
        adapter._native_submit_restart_recovered_profiles.clear()
        monkeypatch.setattr(sync, "_ENDED", collections.deque(maxlen=256))
        await runner._recover_unclean_sessions()
        assert await adapter.connect()
        assert writer.events.empty(), "a second boot must not emit the failure again"
    finally:
        await adapter.disconnect()
        db.close()


@pytest.mark.asyncio
async def test_clean_connect_does_not_fail_an_admitted_turn(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("weave-clean", "api_server")
    db.register_native_session_submit("weave-clean", external_request_id="clean", message_sha256="x", native_request_ref="clean")
    db.set_native_session_submit_admission(native_request_ref="clean", admission="streaming")
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._session_db = db
    adapter.gateway_runner = SimpleNamespace(config=GatewayConfig())
    monkeypatch.setattr(adapter, "_api_key_passes_startup_guard", lambda: True)
    monkeypatch.setattr(adapter, "_wire_plugin_handlers", lambda app: None)
    monkeypatch.setattr(adapter, "_publish_runtime_status", lambda: None)
    monkeypatch.setattr("gateway.platforms.api_server.start_tcp_site", AsyncMock(return_value=SimpleNamespace(stop=AsyncMock())))
    published = []
    monkeypatch.setattr(sync, "publish", lambda *args, **kw: published.append(args))
    try:
        assert await adapter.connect()
        assert published == []
    finally:
        await adapter.disconnect()
        db.close()
