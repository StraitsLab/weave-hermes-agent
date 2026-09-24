"""Durable transcript epoch + post-commit wake-ups on SessionDB (WEV-1817).

``sessions.transcript_epoch`` is bumped by SQLite triggers, inside the writing
transaction, on every non-append change a reader could miss. Wake-ups are
published only after a commit, never on rollback, and carry no data.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from hermes_state import SessionDB


SID = "notes-session"


@pytest.fixture
def db(tmp_path: Path):
    d = SessionDB(tmp_path / "state.db")
    d.create_session(SID, source="test")
    try:
        yield d
    finally:
        d.close()


def _wakes(db: SessionDB):
    calls: list = []
    db.add_commit_listener(lambda: calls.append(db._lock.locked()))
    return calls


# ── wake-ups ─────────────────────────────────────────────────────────────


def test_wake_is_published_after_commit_outside_the_lock(db, tmp_path):
    seen: list = []

    def listener():
        conn = sqlite3.connect(str(tmp_path / "state.db"))  # sees COMMITTED data only
        try:
            seen.append((conn.execute("SELECT content FROM messages").fetchall(), db._lock.locked()))
        finally:
            conn.close()

    db.add_commit_listener(listener)
    db.append_message(SID, role="user", content="durable")
    assert seen == [([("durable",)], False)]


def test_rolled_back_write_wakes_nobody(db, monkeypatch):
    calls = _wakes(db)

    def exploding_insert(conn, session_id, messages):
        SessionDB._insert_message_rows(db, conn, session_id, messages)
        raise RuntimeError("boom inside fn")

    monkeypatch.setattr(db, "_insert_message_rows", exploding_insert)
    with pytest.raises(RuntimeError, match="boom inside fn"):
        db.append_messages_batch(SID, [{"role": "user", "content": "lost"}])
    assert calls == []
    assert db.get_messages(SID) == []


def test_locked_retry_wakes_once(db, monkeypatch):
    calls = _wakes(db)
    attempts = {"n": 0}

    def flaky_insert(conn, session_id, messages):
        attempts["n"] += 1
        if attempts["n"] == 1:
            SessionDB._insert_message_rows(db, conn, session_id, messages)
            raise sqlite3.OperationalError("database is locked")
        return SessionDB._insert_message_rows(db, conn, session_id, messages)

    monkeypatch.setattr(db, "_insert_message_rows", flaky_insert)
    db.append_messages_batch(SID, [{"role": "user", "content": "retried"}])
    assert attempts["n"] == 2
    assert calls == [False]


def test_append_from_other_thread_wakes_listener(db):
    calls = _wakes(db)
    t = threading.Thread(target=db.append_message, args=(SID,), kwargs={"role": "user", "content": "x"})
    t.start()
    t.join(5)
    assert calls == [False]


def test_listeners_are_shared_per_path_and_removable(db, tmp_path):
    other = SessionDB(tmp_path / "state.db")
    try:
        calls: list = []
        listener = lambda: calls.append(1)  # noqa: E731
        db.add_commit_listener(listener)
        assert other.commit_listener_count() == 1
        other.append_message(SID, role="user", content="via second handle")
        assert calls == [1]
        db.remove_commit_listener(listener)
        assert db.commit_listener_count() == 0
        other.append_message(SID, role="user", content="unheard")
        assert calls == [1]
    finally:
        other.close()


# ── epoch: pure appends never bump ───────────────────────────────────────


def test_plain_appends_titles_and_counters_do_not_bump(db):
    before = db.get_transcript_epoch(SID)
    db.append_message(SID, role="user", content="one")
    db.append_message(SID, role="assistant", content="two")
    db.append_messages_batch(SID, [{"role": "user", "content": "three"}])
    db.set_session_title(SID, "a title")
    db.touch_session_activity(SID)
    assert db.get_transcript_epoch(SID) == before


def test_reaction_metadata_does_not_bump(db):
    row = db.append_message(SID, role="assistant", content="hi")
    before = db.get_transcript_epoch(SID)
    with db._lock:
        db._conn.execute("UPDATE messages SET display_metadata = '{\"r\":1}' WHERE id = ?", (row,))
        db._conn.commit()
    assert db.get_transcript_epoch(SID) == before


# ── epoch: every non-append change bumps, durably ────────────────────────


def _bumps(db, sid, change):
    before = db.get_transcript_epoch(sid)
    change()
    return db.get_transcript_epoch(sid) > before


def test_rewind_restore_compaction_and_clear_bump(db):
    one = db.append_message(SID, role="user", content="one")
    db.append_message(SID, role="assistant", content="two")
    assert _bumps(db, SID, lambda: db.rewind_to_message(SID, one))
    assert _bumps(db, SID, lambda: db.restore_rewound(SID, one))
    assert _bumps(db, SID, lambda: db.archive_and_compact(SID, [{"role": "user", "content": "summary"}]))
    assert _bumps(db, SID, lambda: db.clear_messages(SID))


def test_in_place_repair_and_display_kind_stamp_bump(db):
    row = db.append_message(SID, role="assistant", content="")
    assert _bumps(db, SID, lambda: db.append_messages_batch(
        SID, [{"role": "assistant", "content": "final answer", "_row_id": row}]))
    db.append_message(SID, role="user", content="synthetic prompt")
    assert _bumps(db, SID, lambda: db.set_latest_matching_message_display_kind(
        SID, role="user", content="synthetic prompt", display_kind="internal"))


def test_compression_fork_bumps_parent(db):
    db.append_message(SID, role="user", content="before fork")
    assert _bumps(db, SID, lambda: db.publish_compression_child(
        parent_session_id=SID, child_session_id="notes-child", source="test",
        messages=[{"role": "user", "content": "handoff"}], require_compression_lease=False))
    assert db.resolve_resume_session_id(SID) == "notes-child"


def test_ending_or_deleting_an_eligible_child_bumps_the_root(db):
    db.append_message(SID, role="user", content="root")
    db.create_session("a", "test", parent_session_id=SID)
    db.append_message("a", role="user", content="a")
    assert _bumps(db, SID, lambda: db.end_session("a", "agent_close"))
    db.create_session("empty", "test", parent_session_id=SID)
    assert _bumps(db, SID, lambda: db.delete_session_if_empty("empty"))


def test_import_of_a_child_bumps_the_parent(db):
    db.append_message(SID, role="user", content="root")
    assert _bumps(db, SID, lambda: db.import_sessions([{
        "id": "imported", "source": "test", "parent_session_id": SID,
        "messages": [{"role": "user", "content": "imported continuation"}]}]))


def test_branch_and_delegate_children_do_not_bump(db):
    db.append_message(SID, role="user", content="root")
    before = db.get_transcript_epoch(SID)
    db.create_session("br", "test", parent_session_id=SID, model_config={"_branched_from": SID})
    db.create_session("tool-child", "tool", parent_session_id=SID)
    assert db.get_transcript_epoch(SID) == before


def test_epoch_survives_reopen(tmp_path):
    first = SessionDB(tmp_path / "state.db")
    first.create_session(SID, "test")
    row = first.append_message(SID, role="user", content="one")
    first.rewind_to_message(SID, row)
    epoch = first.get_transcript_epoch(SID)
    first.close()
    second = SessionDB(tmp_path / "state.db")
    try:
        assert epoch > 0 and second.get_transcript_epoch(SID) == epoch
    finally:
        second.close()


def test_raw_write_from_another_connection_still_bumps(db, tmp_path):
    row = db.append_message(SID, role="user", content="one")
    before = db.get_transcript_epoch(SID)
    raw = sqlite3.connect(str(tmp_path / "state.db"))
    try:
        raw.execute("UPDATE messages SET content = 'edited elsewhere' WHERE id = ?", (row,))
        raw.commit()
    finally:
        raw.close()
    assert db.get_transcript_epoch(SID) > before


def test_raw_child_insert_from_another_connection_bumps_the_parent(db, tmp_path):
    """The session INSERT trigger alone (a plain INSERT fires no UPDATE trigger)."""
    db.append_message(SID, role="user", content="root")
    before = db.get_transcript_epoch(SID)
    raw = sqlite3.connect(str(tmp_path / "state.db"))
    try:
        raw.execute("INSERT INTO sessions (id, source, parent_session_id, started_at) VALUES ('raw-child', 'test', ?, 1)", (SID,))
        raw.commit()
    finally:
        raw.close()
    assert db.get_transcript_epoch(SID) > before


def test_existing_database_gains_triggers_and_starts_at_epoch_zero(tmp_path):
    path = tmp_path / "state.db"
    first = SessionDB(path)
    first.create_session(SID, "test")
    first.append_message(SID, role="user", content="legacy")
    first.close()
    raw = sqlite3.connect(str(path))
    try:
        for (name,) in raw.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'transcript_epoch_%'").fetchall():
            raw.execute(f"DROP TRIGGER {name}")
        raw.execute("UPDATE sessions SET transcript_epoch = 0")
        raw.commit()
    finally:
        raw.close()
    reopened = SessionDB(path)
    try:
        assert reopened.get_transcript_epoch(SID) == 0
        row = reopened.get_messages(SID)[0]["id"]
        assert _bumps(reopened, SID, lambda: reopened.rewind_to_message(SID, row))
    finally:
        reopened.close()


# ── round-2 review: raw-SQL paths the triggers must still catch ──────────


def _raw(tmp_path, *statements):
    conn = sqlite3.connect(str(tmp_path / "state.db"))
    try:
        for sql, args in statements:
            conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def test_insert_or_replace_over_a_row_bumps(db, tmp_path):
    row = db.append_message(SID, role="user", content="original")
    before = db.get_transcript_epoch(SID)
    _raw(tmp_path, ("INSERT OR REPLACE INTO messages(id, session_id, role, content, timestamp) "
                    "VALUES (?, ?, 'user', 'replacement', 1)", (row, SID)))
    assert db.get_messages(SID)[0]["content"] == "replacement"
    assert db.get_transcript_epoch(SID) > before


def test_backfill_insert_below_the_head_bumps(db, tmp_path):
    one = db.append_message(SID, role="user", content="one")
    two = db.append_message(SID, role="user", content="two")
    three = db.append_message(SID, role="user", content="three")
    _raw(tmp_path, ("DELETE FROM messages WHERE id = ?", (two,)))
    before = db.get_transcript_epoch(SID)
    _raw(tmp_path, ("INSERT INTO messages(id, session_id, role, content, timestamp, active) "
                    "VALUES (?, ?, 'user', 'two', 1, 1)", (two, SID)))
    assert [r["id"] for r in db.get_messages(SID)] == [one, two, three]
    assert db.get_transcript_epoch(SID) > before


@pytest.mark.parametrize("sql", [
    "UPDATE messages SET id = 100 WHERE id = ?",
    "UPDATE messages SET rowid = 100 WHERE id = ?",
    "UPDATE messages SET oid = 100 WHERE id = ?",
    "UPDATE messages SET _rowid_ = 100 WHERE id = ?",
])
def test_message_ids_are_immutable(db, tmp_path, sql):
    first = db.append_message(SID, role="user", content="one")
    db.append_message(SID, role="user", content="two")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        _raw(tmp_path, (sql, (first,)))
    assert [r["content"] for r in db.get_messages(SID)] == ["one", "two"]


def test_update_or_replace_onto_another_id_is_refused(db, tmp_path):
    kept = db.append_message(SID, role="user", content="will not vanish")
    db.create_session("other", "test")
    moved = db.append_message("other", role="user", content="moved")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        _raw(tmp_path, ("UPDATE OR REPLACE messages SET id = ? WHERE id = ?", (kept, moved)))
    assert [r["id"] for r in db.get_messages(SID)] == [kept]


def test_appends_after_a_legacy_negative_id_row_do_not_bump(db, tmp_path):
    # A legacy row written before the positive-id rule (the rule rejects new ones).
    _raw(tmp_path, ("DROP TRIGGER transcript_epoch_message_positive_id", ()),
         ("INSERT INTO messages(id, session_id, role, content, timestamp, active) "
          "VALUES (-1, ?, 'user', 'imported', 1, 1)", (SID,)))
    reopened = SessionDB(tmp_path / "state.db")  # reinstalls the rule; the legacy row stays
    reopened.close()
    before = db.get_transcript_epoch(SID)
    db.append_message(SID, role="user", content="ordinary append")
    assert db.get_transcript_epoch(SID) == before


def test_a_tail_append_with_an_explicit_id_does_not_bump(db, tmp_path):
    last = db.append_message(SID, role="user", content="one")
    before = db.get_transcript_epoch(SID)
    _raw(tmp_path, ("INSERT INTO messages(id, session_id, role, content, timestamp, active) "
                    "VALUES (?, ?, 'user', 'tail', 1, 1)", (last + 5, SID)))
    assert db.get_transcript_epoch(SID) == before


def test_trigger_install_waits_out_a_held_write_lock(tmp_path):
    """Round-2 finding 1: a lock during the trigger install used to be logged and
    swallowed, opening the database with NO triggers. It must retry instead."""
    path = tmp_path / "state.db"
    first = SessionDB(path)
    first.create_session(SID, "test")
    row = first.append_message(SID, role="user", content="x")
    first.close()
    holder = sqlite3.connect(str(path), check_same_thread=False)
    for (name,) in holder.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'transcript_epoch_%'").fetchall():
        holder.execute(f"DROP TRIGGER {name}")
    holder.commit()
    holder.execute("BEGIN IMMEDIATE")
    release = threading.Timer(2.5, holder.rollback)  # past the open's 1 s busy timeout
    release.start()
    try:
        reopened = SessionDB(path)
    finally:
        release.join()
        holder.close()
    try:
        with reopened._lock:
            installed = reopened._conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'transcript_epoch_%'"
            ).fetchone()[0]
        from hermes_state_common import TRANSCRIPT_TRIGGERS
        assert installed == len(TRANSCRIPT_TRIGGERS)
        before = reopened.get_transcript_epoch(SID)
        reopened._execute_write(lambda c: c.execute("UPDATE messages SET content = 'edited' WHERE id = ?", (row,)))
        assert reopened.get_transcript_epoch(SID) > before
    finally:
        reopened.close()


# ── round-3 review: upgrades and replaced identities ─────────────────────


def test_stale_trigger_bodies_are_rebuilt_on_open(tmp_path):
    """An upgraded database keeps whatever trigger body CREATE TRIGGER IF NOT
    EXISTS found; the open must replace a body that differs from the current one."""
    path = tmp_path / "state.db"
    first = SessionDB(path)
    first.create_session(SID, "test")
    first.close()
    raw = sqlite3.connect(str(path))
    try:
        raw.execute("DROP TRIGGER transcript_epoch_message_replace")
        raw.execute("CREATE TRIGGER transcript_epoch_message_replace BEFORE INSERT ON messages "
                    "WHEN NEW.id IS NOT NULL BEGIN UPDATE sessions SET transcript_epoch = transcript_epoch + 1; END")
        raw.execute("CREATE TRIGGER transcript_epoch_retired AFTER INSERT ON messages BEGIN SELECT 1; END")
        raw.commit()
    finally:
        raw.close()
    reopened = SessionDB(path)
    try:
        with reopened._lock:
            names = {name for (name,) in reopened._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'transcript_epoch_%'")}
            body = reopened._conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'transcript_epoch_message_replace'").fetchone()[0]
        from hermes_state_common import TRANSCRIPT_TRIGGERS
        assert names == set(TRANSCRIPT_TRIGGERS)  # the retired trigger is gone
        assert "NEW.id > 0" in body
        before = reopened.get_transcript_epoch(SID)
        reopened.append_message(SID, role="user", content="ordinary")
        assert reopened.get_transcript_epoch(SID) == before
    finally:
        reopened.close()


def test_new_message_ids_must_be_positive(db, tmp_path):
    for value in (0, -1):
        with pytest.raises(sqlite3.IntegrityError, match="positive"):
            _raw(tmp_path, ("INSERT INTO messages(id, session_id, role, content, timestamp) "
                            "VALUES (?, ?, 'user', 'x', 1)", (value, SID)))
    assert db.get_messages(SID) == []


def test_replacing_a_session_record_that_owns_messages_bumps(db, tmp_path):
    first = db.append_message(SID, role="user", content="one")
    db.append_message(SID, role="user", content="two")
    db._execute_write(lambda c: c.execute("UPDATE messages SET content = 'edited' WHERE id = ?", (first,)))
    before = db.get_transcript_epoch(SID)
    assert before > 0
    conn = sqlite3.connect(str(tmp_path / "state.db"))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("INSERT OR REPLACE INTO sessions(id, source, started_at) VALUES (?, 'test', 1)", (SID,))
        conn.commit()
    finally:
        conn.close()
    assert [r["content"] for r in db.get_messages(SID)] == ["edited", "two"]
    assert db.get_transcript_epoch(SID) > before  # not rolled back to the column default


def test_creating_an_empty_session_does_not_bump_via_reinsert(db):
    before = db.get_transcript_epoch(SID)
    db.create_session("fresh", "test")
    assert db.get_transcript_epoch(SID) == before  # a new session never bumps an unrelated one


# ── round-4 review: session identity ───────────────────────────────────────


def test_session_ids_are_immutable(db):
    db.append_message(SID, role="user", content="one")
    db.create_session("spare", "test")
    for sql in ("UPDATE sessions SET id = 'moved' WHERE id = ?",
                "UPDATE OR REPLACE sessions SET id = ? WHERE id = 'spare'"):
        with pytest.raises(sqlite3.IntegrityError, match="sessions.id is immutable"):
            db._execute_write(lambda c, sql=sql: c.execute(sql, (SID,)))
    assert [r["content"] for r in db.get_messages(SID)] == ["one"]


def test_a_recreated_session_never_reissues_an_old_epoch(db):
    first = db.append_message(SID, role="user", content="one")
    db.append_message(SID, role="user", content="two")
    old = db.get_transcript_epoch(SID)
    db._execute_write(lambda c: (c.execute("DELETE FROM messages WHERE session_id = ?", (SID,)),
                                 c.execute("DELETE FROM sessions WHERE id = ?", (SID,))))
    db.create_session(SID, "test")
    db._execute_write(lambda c: c.execute(
        "INSERT INTO messages(id, session_id, role, content, timestamp) VALUES (?, ?, 'user', 'changed', 1)",
        (first, SID)))
    page = db.read_transcript_events(SID, (old, first), 50)
    assert page["epoch"] != old and page["reset_required"]
