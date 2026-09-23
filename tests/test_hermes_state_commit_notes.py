"""Post-commit transcript notes on SessionDB (WEV-1817).

A note is published only after ``_execute_write`` commits, never on rollback,
and exactly once across a ``database is locked`` retry. Notes carry no row
content — subscribers re-read the database.
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


def _collect(db: SessionDB):
    notes: list = []
    db.add_commit_listener(notes.append)
    return notes


def test_note_is_published_after_commit_and_row_is_readable_in_callback(db, tmp_path):
    seen: list = []

    def listener(note):
        # An independent connection only sees COMMITTED data.
        conn = sqlite3.connect(str(tmp_path / "state.db"))
        try:
            row = conn.execute(
                "SELECT content FROM messages WHERE id = ?", (note[2],)
            ).fetchone()
        finally:
            conn.close()
        seen.append((note, row, db._lock.locked()))

    db.add_commit_listener(listener)
    msg_id = db.append_message(SID, role="user", content="durable")

    assert len(seen) == 1
    note, row, lock_held = seen[0]
    assert note == ("appended", SID, msg_id)
    assert row == ("durable",)
    assert lock_held is False, "listeners run outside the SessionDB write lock"


def test_rolled_back_write_publishes_nothing(db, monkeypatch):
    notes = _collect(db)

    def exploding_insert(conn, session_id, messages):
        # Real insert (which records a note), then fail inside fn.
        SessionDB._insert_message_rows(db, conn, session_id, messages)
        raise RuntimeError("boom inside fn")

    monkeypatch.setattr(db, "_insert_message_rows", exploding_insert)
    with pytest.raises(RuntimeError, match="boom inside fn"):
        db.append_messages_batch(SID, [{"role": "user", "content": "lost"}])

    assert notes == []
    assert db.get_messages(SID) == []


def test_locked_retry_publishes_once(db, monkeypatch):
    notes = _collect(db)
    attempts = {"n": 0}

    def flaky_insert(conn, session_id, messages):
        attempts["n"] += 1
        if attempts["n"] == 1:
            # The doomed attempt notes a DIFFERENT max id, so a note leaked
            # from it could not be hidden by value de-duplication.
            SessionDB._insert_message_rows(
                db, conn, session_id, [*messages, {"role": "user", "content": "doomed"}]
            )
            raise sqlite3.OperationalError("database is locked")
        return SessionDB._insert_message_rows(db, conn, session_id, messages)

    monkeypatch.setattr(db, "_insert_message_rows", flaky_insert)
    db.append_messages_batch(SID, [{"role": "user", "content": "retried"}])

    assert attempts["n"] == 2
    rows = db.get_messages(SID)
    assert [r["content"] for r in rows] == ["retried"]
    assert notes == [("appended", SID, rows[0]["id"])]


def test_append_from_other_thread_notifies_listener(db):
    notes = _collect(db)
    t = threading.Thread(
        target=db.append_message, args=(SID,), kwargs={"role": "user", "content": "x"}
    )
    t.start()
    t.join(5)
    assert len(notes) == 1 and notes[0][0] == "appended" and notes[0][1] == SID


def test_rewind_and_compaction_note_reset(db):
    first = db.append_message(SID, role="user", content="one")
    db.append_message(SID, role="assistant", content="two")
    notes = _collect(db)

    db.rewind_to_message(SID, first)
    assert ("reset", SID) in notes

    notes.clear()
    db.append_message(SID, role="user", content="three")
    db.archive_and_compact(SID, [{"role": "user", "content": "summary"}])
    assert ("reset", SID) in notes
    assert notes[-1][0] == "appended", "compacted rows are announced after the reset"


def test_compression_fork_notes_reset_on_parent(db):
    db.append_message(SID, role="user", content="before fork")
    notes = _collect(db)
    db.publish_compression_child(
        parent_session_id=SID,
        child_session_id="notes-child",
        source="test",
        messages=[{"role": "user", "content": "handoff"}],
        require_compression_lease=False,
    )
    assert ("reset", SID) in notes
    assert any(n[0] == "appended" and n[1] == "notes-child" for n in notes)
    assert db.resolve_resume_session_id(SID) == "notes-child"


def test_remove_listener_stops_delivery_and_is_shared_per_path(db, tmp_path):
    other = SessionDB(tmp_path / "state.db")
    try:
        notes: list = []
        db.add_commit_listener(notes.append)
        assert other.commit_listener_count() == 1
        other.append_message(SID, role="user", content="via second handle")
        assert len(notes) == 1
        db.remove_commit_listener(notes.append)
        assert db.commit_listener_count() == 0
        other.append_message(SID, role="user", content="unheard")
        assert len(notes) == 1
    finally:
        other.close()
