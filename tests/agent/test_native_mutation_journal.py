from __future__ import annotations

import json

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


def test_failure_record_is_structured_and_content_free(tmp_path):
    journal = NativeMutationJournal(tmp_path)
    journal.begin("op-1")
    journal.fail("op-1", "writer exploded")
    result = journal.begin("op-1")
    assert result == {
        "success": False,
        "operation_id": "op-1",
        "error": "native_mutation_failed",
        "detail": "writer exploded",
    }
    raw = json.loads((tmp_path / "memories" / "native-operation-journal.json").read_text())
    assert raw["records"][0]["operation_id"] == "op-1"
    assert "content" not in raw["records"][0]


def test_terminal_records_are_bounded(tmp_path):
    journal = NativeMutationJournal(tmp_path)
    for i in range(300):
        op = f"op-{i}"
        journal.begin(op)
        journal.complete(op, {"success": True, "operation_id": op})
    payload = json.loads((tmp_path / "memories" / "native-operation-journal.json").read_text())
    assert len(payload["records"]) == 256
