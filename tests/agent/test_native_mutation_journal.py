from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from agent.native_mutation_journal import NativeMutationJournal

def test_pending_then_completed_retry_returns_exact_result(tmp_path):
    journal = NativeMutationJournal(tmp_path)
    assert journal.begin("op-1") is None
    result = {"success": True, "operation_id": "op-1", "revision": 7}
    journal.complete("op-1", result)
    assert journal.begin("op-1") == result


def test_orphan_pending_fails_closed(tmp_path):
    journal = NativeMutationJournal(tmp_path)
    journal.begin("op-1")
    with pytest.raises(RuntimeError, match="native_operation_in_doubt"):
        journal.begin("op-1")


def test_journal_reuses_portable_memory_store_lock(tmp_path, monkeypatch):
    from tools.memory_tool import MemoryStore

    locked = []

    @contextmanager
    def portable_lock(path):
        locked.append(path)
        yield

    monkeypatch.setattr(MemoryStore, "_file_lock", portable_lock)
    journal = NativeMutationJournal(tmp_path)
    journal.begin("op-portable")
    assert locked == [journal.path]


def test_failure_record_is_structured_and_content_free(tmp_path):
    journal = NativeMutationJournal(tmp_path)
    journal.begin("op-1")
    journal.fail("op-1", "writer exploded")
    result = journal.begin("op-1")
    assert result == {"success": False, "operation_id": "op-1", "error": "native_mutation_failed"}
    raw = json.loads((tmp_path / "memories" / "native-operation-journal.json").read_text())
    assert raw["records"][0]["operation_id"] == "op-1"
    assert "content" not in raw["records"][0]


def test_hostile_terminal_result_is_compact_in_replay_and_bytes(tmp_path):
    sentinel = "HOSTILE MEMORY TEXT"
    journal = NativeMutationJournal(tmp_path)
    journal.begin("op-hostile")
    journal.complete("op-hostile", {
        "success": False,
        "operation_id": "op-hostile",
        "status": "conflict",
        "revision": 9,
        "error": sentinel,
        "current_entries": [sentinel],
        "message": sentinel,
        "provider_status": "failed",
    })

    assert journal.begin("op-hostile") == {
        "success": False,
        "operation_id": "op-hostile",
        "revision": 9,
        "status": "conflict",
        "provider_status": "failed",
        "error": "native_mutation_failed",
    }
    assert sentinel not in journal.path.read_text(encoding="utf-8")


@pytest.mark.parametrize("record", [
    "{broken",
    {"operation_id": "op-1", "status": "completed"},
    {"operation_id": "op-1", "status": "completed", "result": []},
    {"operation_id": "op-1", "status": "completed", "result": {"success": True, "operation_id": "other"}},
    {"operation_id": "op-1", "status": "completed", "result": {"success": 1, "operation_id": "op-1"}},
    {"operation_id": "op-1", "status": "completed", "result": {"success": True, "operation_id": "op-1", "current_entries": ["SENTINEL"]}},
    {"operation_id": "op-1", "status": "pending", "result": {"success": True, "operation_id": "op-1"}},
])
def test_corrupt_journal_fails_closed_without_overwriting(tmp_path, record):
    journal = NativeMutationJournal(tmp_path)
    journal.directory.mkdir(parents=True, exist_ok=True)
    original = record if isinstance(record, str) else json.dumps({"records": [record]})
    journal.path.write_text(original, encoding="utf-8")
    with pytest.raises(RuntimeError, match="native_operation_in_doubt"):
        journal.begin("op-1")
    assert journal.path.read_text(encoding="utf-8") == original

def test_terminal_records_are_bounded(tmp_path):
    journal = NativeMutationJournal(tmp_path)
    for i in range(300):
        op = f"op-{i}"
        journal.begin(op)
        journal.complete(op, {"success": True, "operation_id": op})
    payload = json.loads((tmp_path / "memories" / "native-operation-journal.json").read_text())
    assert len(payload["records"]) == 256


@pytest.mark.windows_only
def test_windows_journal_lifecycle_skips_directory_descriptor(tmp_path, monkeypatch):
    real_open = __import__("os").open
    def reject_directory_open(path, flags, *args, **kwargs):
        if path == tmp_path / "memories":
            raise AssertionError("Windows journal must not open the directory")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("agent.native_mutation_journal.os.open", reject_directory_open)
    journal = NativeMutationJournal(tmp_path)
    assert journal.begin("op-windows") is None
    journal.complete("op-windows", {"success": True, "revision": 11})
    assert journal.begin("op-windows") == {"success": True, "operation_id": "op-windows", "revision": 11}
