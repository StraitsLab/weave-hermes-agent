"""Harso execution cursor behavior ported from the pre-v0.22 fork."""
import pytest
from cron import executions


def test_history_cursor_keeps_same_timestamp_rows(monkeypatch, tmp_path):
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "executions.sqlite")
    from datetime import datetime
    monkeypatch.setattr(executions, "_hermes_now", lambda: datetime.fromisoformat("2026-10-15T00:00:00+00:00"))
    for _ in range(3):
        executions.create_execution("cursor-job", source="builtin")
    first, cursor, has_more = executions.list_execution_page("cursor-job", limit=2)
    second, next_cursor, more = executions.list_execution_page("cursor-job", limit=2, before_claimed_at=cursor)
    assert len(first) == 2 and len(second) == 1
    assert has_more and not more and next_cursor is None
    assert len({row["id"] for row in first + second}) == 3


@pytest.mark.parametrize("cursor", ["bad|", "|execution", "not-a-time|execution", "a|b|c"])
def test_history_cursor_rejects_malformed_compound_values(monkeypatch, tmp_path, cursor):
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "executions.sqlite")
    with pytest.raises(ValueError):
        executions.list_execution_page("cursor-job", before_claimed_at=cursor)
