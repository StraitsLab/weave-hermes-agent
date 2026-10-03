"""Text-only pages survive compact browser snapshots."""

import json
from unittest.mock import Mock

import pytest

from tools import browser_tool


TASK_ID = "compact-fallback-test"
TEXT_ONLY_SNAPSHOT = (
    'StaticText "missing signature / signature-input / signature-agent headers"'
)


@pytest.fixture(autouse=True)
def local_snapshot(monkeypatch):
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(browser_tool, "_is_local_backend", lambda: True)
    monkeypatch.setattr(browser_tool, "_last_session_key", lambda task_id: task_id)


def _snapshot_result(snapshot, refs=None):
    return {"success": True, "data": {"snapshot": snapshot, "refs": refs or {}}}


@pytest.mark.parametrize(
    "compact",
    ["(empty page)", "", " \n\t", " \n(empty page)\t"],
    ids=["marker", "empty-string", "whitespace", "padded"],
)
def test_compact_empty_page_returns_full_snapshot_text(monkeypatch, compact):
    command = Mock(
        side_effect=[
            _snapshot_result(compact),
            _snapshot_result(TEXT_ONLY_SNAPSHOT),
        ]
    )
    monkeypatch.setattr(browser_tool, "_run_browser_command", command)

    result = json.loads(browser_tool.browser_snapshot(task_id=TASK_ID))

    assert result["success"] is True
    assert result["snapshot"] == TEXT_ONLY_SNAPSHOT
    assert result["element_count"] == 0
    assert [call.args for call in command.call_args_list] == [
        (TASK_ID, "snapshot", ["-c"]),
        (TASK_ID, "snapshot", []),
    ]


def test_compact_content_without_refs_does_not_retry(monkeypatch):
    command = Mock(return_value=_snapshot_result(TEXT_ONLY_SNAPSHOT))
    monkeypatch.setattr(browser_tool, "_run_browser_command", command)

    result = json.loads(browser_tool.browser_snapshot(task_id=TASK_ID))

    assert result["success"] is True
    assert result["snapshot"] == TEXT_ONLY_SNAPSHOT
    command.assert_called_once_with(TASK_ID, "snapshot", ["-c"])


def test_explicit_full_empty_snapshot_does_not_retry(monkeypatch):
    command = Mock(return_value=_snapshot_result("(empty page)"))
    monkeypatch.setattr(browser_tool, "_run_browser_command", command)

    result = json.loads(browser_tool.browser_snapshot(full=True, task_id=TASK_ID))

    assert result["success"] is True
    assert result["snapshot"] == "(empty page)"
    command.assert_called_once_with(TASK_ID, "snapshot", [])


def test_compact_and_full_empty_snapshots_stop_after_one_retry(monkeypatch):
    command = Mock(
        side_effect=[_snapshot_result(""), _snapshot_result("(empty page)")]
    )
    monkeypatch.setattr(browser_tool, "_run_browser_command", command)

    result = json.loads(browser_tool.browser_snapshot(task_id=TASK_ID))

    assert result["success"] is True
    assert result["snapshot"] == "(empty page)"
    assert command.call_count == 2


def test_failed_full_snapshot_keeps_compact_result(monkeypatch):
    command = Mock(
        side_effect=[
            _snapshot_result("(empty page)"),
            {"success": False, "error": "Full snapshot unavailable"},
        ]
    )
    monkeypatch.setattr(browser_tool, "_run_browser_command", command)

    result = json.loads(browser_tool.browser_snapshot(task_id=TASK_ID))

    assert result["success"] is True
    assert result["snapshot"] == "(empty page)"
    assert command.call_count == 2


def test_fallback_uses_full_snapshot_refs(monkeypatch):
    full_snapshot = 'button "Continue" [ref=e1]'
    command = Mock(
        side_effect=[
            _snapshot_result("(empty page)"),
            _snapshot_result(full_snapshot, {"e1": {}}),
        ]
    )
    monkeypatch.setattr(browser_tool, "_run_browser_command", command)

    result = json.loads(browser_tool.browser_snapshot(task_id=TASK_ID))

    assert result["success"] is True
    assert result["snapshot"] == full_snapshot
    assert result["element_count"] == 1
    assert command.call_count == 2


def test_failed_compact_snapshot_does_not_retry(monkeypatch):
    command = Mock(return_value={"success": False, "error": "Snapshot unavailable"})
    monkeypatch.setattr(browser_tool, "_run_browser_command", command)

    result = json.loads(browser_tool.browser_snapshot(task_id=TASK_ID))

    assert result["success"] is False
    assert result["error"] == "Snapshot unavailable"
    command.assert_called_once_with(TASK_ID, "snapshot", ["-c"])
