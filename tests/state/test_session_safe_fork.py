"""Atomic native session-fork boundary contracts."""

import json

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    state = SessionDB(tmp_path / "state.db")
    state.create_session("predecessor", "api_server", model="model-a", model_config={"keep": True})
    state.append_message("predecessor", "user", "hello")
    state.append_message("predecessor", "assistant", "hi")
    try:
        yield state
    finally:
        state.close()


def test_safe_fork_is_atomic_lineage_record_and_identical_replay(db):
    revoked = []

    first = db.safe_fork_session("predecessor", "weave-successor", before_commit=lambda: revoked.append(True))
    retry = db.safe_fork_session("predecessor", "weave-successor", before_commit=lambda: revoked.append(True))

    assert first == "forked"
    assert retry == "identical_retry"
    assert revoked == [True, True]
    parent = db.get_session("predecessor")
    child = db.get_session("weave-successor")
    assert parent["end_reason"] == "branched"
    assert child["parent_session_id"] == "predecessor"
    assert json.loads(child["model_config"]) == {
        "keep": True,
        "_branched_from": "predecessor",
    }
    assert [m["content"] for m in db.get_messages("weave-successor")] == ["hello", "hi"]


@pytest.mark.parametrize("lock_kind", ["compression", "turn"])
def test_safe_fork_refuses_native_durable_locks_without_revoking(db, lock_kind):
    if lock_kind == "compression":
        assert db.try_acquire_compression_lock("predecessor", "compressor")
    else:
        assert db.try_acquire_session_turn_lease("predecessor", "turn-holder")
    revoked = []

    result = db.safe_fork_session("predecessor", "weave-successor", before_commit=lambda: revoked.append(True))

    assert result == "boundary_unavailable"
    assert revoked == []
    assert db.get_session("predecessor")["ended_at"] is None
    assert db.get_session("weave-successor") is None


def test_safe_fork_conflicts_with_existing_child_or_changed_source(db):
    db.create_session("somebody-else", "api_server")
    db.create_session("weave-successor", "api_server", parent_session_id="somebody-else")
    assert db.safe_fork_session("predecessor", "weave-successor") == "conflict"
    db.delete_session("weave-successor")
    db.end_session("predecessor", "closed")
    assert db.safe_fork_session("predecessor", "weave-successor") == "conflict"


def test_safe_fork_rolls_back_all_database_changes_after_precommit_revoke(db, monkeypatch):
    revoked = []
    monkeypatch.setattr(db, "_message_column_names", lambda _conn: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        db.safe_fork_session("predecessor", "weave-successor", before_commit=lambda: revoked.append(True))

    assert revoked == [True]
    assert db.get_session("predecessor")["ended_at"] is None
    assert db.get_session("weave-successor") is None
