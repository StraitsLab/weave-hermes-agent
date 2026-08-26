"""Behavior tests for the built-in memory → external provider bridge.

The bridge lives behind the MemoryManager interface
(``MemoryManager.notify_memory_tool_write``): the agent loop hands over the raw
built-in memory tool result + args, and the manager decides whether/what to
mirror to external providers. These tests drive that method with a fake
external provider and assert which ``on_memory_write`` calls land.
"""

import json

import pytest
from agent.native_mutation_journal import NativeMutationJournal

from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider


class _RecordingProvider(MemoryProvider):
    """Minimal external provider that records on_memory_write calls."""

    def __init__(self) -> None:
        self.calls = []

    @property
    def name(self) -> str:
        return "recording"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        pass

    def get_tool_schemas(self):
        return []

    def shutdown(self) -> None:
        pass

    def on_memory_write(self, action, target, content, metadata=None):
        self.calls.append({
            "action": action,
            "target": target,
            "content": content,
            "metadata": dict(metadata or {}),
        })


def _manager_with_provider():
    mgr = MemoryManager()
    provider = _RecordingProvider()
    mgr.add_provider(provider)
    return mgr, provider


def test_notifies_remove_with_old_text_after_success():
    mgr, provider = _manager_with_provider()
    mgr.notify_memory_tool_write(
        json.dumps({"success": True}),
        {"action": "remove", "target": "memory", "old_text": "stale preference entry"},
    )
    assert provider.calls == [
        {
            "action": "remove",
            "target": "memory",
            "content": "",
            "metadata": {"old_text": "stale preference entry"},
        }
    ]






@pytest.mark.parametrize("tool_result", [None, [], object(), "not-json"])
def test_skips_unrecognized_tool_result_shape(tool_result):
    mgr, provider = _manager_with_provider()
    mgr.notify_memory_tool_write(
        tool_result,
        {"action": "add", "target": "memory", "content": "new fact"},
    )
    assert provider.calls == []






def test_build_metadata_callback_is_merged_per_op():
    mgr, provider = _manager_with_provider()
    mgr.notify_memory_tool_write(
        json.dumps({"success": True}),
        {"action": "add", "target": "memory", "content": "fact"},
        build_metadata=lambda: {"session_id": "s1", "tool_name": "memory"},
    )
    assert provider.calls == [
        {
            "action": "add",
            "target": "memory",
            "content": "fact",
            "metadata": {"session_id": "s1", "tool_name": "memory"},
        }
    ]


def test_native_mutation_reuses_operation_and_calls_provider_after_commit():
    mgr, provider = _manager_with_provider()
    seen = []

    def write():
        seen.append("native")
        return {"success": True, "revision": 4, "value": "fact"}

    first = mgr.commit_native_mutation("op-1", "add", "memory", "fact", write)
    second = mgr.commit_native_mutation("op-1", "add", "memory", "fact", write)

    assert first == second
    assert seen == ["native"]
    assert provider.calls[0]["metadata"]["operation_id"] == "op-1"
    assert provider.calls[0]["metadata"]["revision"] == 4


def test_native_mutation_reports_native_commit_when_provider_fails():
    mgr, provider = _manager_with_provider()
    provider.on_memory_write = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("down"))
    result = mgr.commit_native_mutation(
        "op-2", "replace", "memory", "new", lambda: {"success": True, "revision": 5}
    )
    assert result["success"] is True
    assert result["provider_acknowledged"] is False


def test_native_writer_failure_is_replayable_without_keyerror(tmp_path):
    mgr = MemoryManager(native_journal=NativeMutationJournal(tmp_path))
    calls = []

    def write():
        calls.append(1)
        raise ValueError("disk broke")

    first = mgr.commit_native_mutation("op-fail", "add", "memory", "x", write)
    second = mgr.commit_native_mutation("op-fail", "add", "memory", "x", write)
    assert first == second
    assert first["error"] == "native_mutation_failed"
    assert calls == [1]
