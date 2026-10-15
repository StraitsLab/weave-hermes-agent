"""Behavior tests for the built-in memory → external provider bridge.

The bridge lives behind the MemoryManager interface
(``MemoryManager.notify_memory_tool_write``): the agent loop hands over the raw
built-in memory tool result + args, and the manager decides whether/what to
mirror to external providers. These tests drive that method with a fake
external provider and assert which ``on_memory_write`` calls land.
"""

import json
import threading

import pytest
from agent.native_mutation_journal import NativeMutationJournal

from agent.memory_manager import MemoryManager, configured_memory_manager
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
        self.initialized = {"session_id": session_id, **kwargs}

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


def test_configured_manager_loads_and_initializes_active_provider(monkeypatch, tmp_path):
    provider = _RecordingProvider()
    monkeypatch.setattr("plugins.memory._get_active_memory_provider", lambda: "recording")
    monkeypatch.setattr("plugins.memory.load_memory_provider", lambda name: provider)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "work")

    manager = configured_memory_manager(session_id="session-1", platform="desktop")
    result = manager.commit_native_mutation(
        "op-configured", "add", "memory", "fact", lambda: {"success": True}
    )

    assert manager.providers == [provider]
    assert result["provider_status"] == "acknowledged"
    assert provider.calls[0]["metadata"]["operation_id"] == "op-configured"
    assert provider.initialized == {
        "session_id": "session-1",
        "platform": "desktop",
        "hermes_home": str(tmp_path),
        "agent_context": "primary",
        "agent_identity": "work",
        "agent_workspace": "hermes",
    }


@pytest.mark.parametrize("provider", [None, _RecordingProvider()])
def test_configured_manager_leaves_unavailable_provider_unconfigured(monkeypatch, provider):
    monkeypatch.setattr("plugins.memory._get_active_memory_provider", lambda: "recording")
    monkeypatch.setattr("plugins.memory.load_memory_provider", lambda name: provider)
    if provider is not None:
        monkeypatch.setattr(provider, "is_available", lambda: False)

    manager = configured_memory_manager()
    result = manager.commit_native_mutation(
        "op-unavailable", "add", "memory", "fact", lambda: {"success": True}
    )

    assert manager.providers == []
    assert result["provider_status"] == "not_configured"
    assert result["provider_acknowledged"] is False


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
    assert "value" not in first


@pytest.mark.parametrize("success", [False, "HOSTILE MEMORY TEXT", 1, None, ...])
def test_hostile_native_failure_is_compact_for_owner_follower_cache_and_journal(tmp_path, success):
    sentinel = "HOSTILE MEMORY TEXT"
    journal = NativeMutationJournal(tmp_path)
    mgr = MemoryManager(native_journal=journal)
    entered = threading.Event()
    release = threading.Event()
    results = []

    def write():
        entered.set()
        assert release.wait(timeout=2)
        result = {"status": "conflict", "revision": 12,
                  "current_entries": [sentinel], "message": sentinel, "error": sentinel}
        if success is not ...: result["success"] = success
        return result

    owner = threading.Thread(target=lambda: results.append(
        mgr.commit_native_mutation("op-hostile", "replace", "memory", sentinel, write)
    ))
    follower = threading.Thread(target=lambda: results.append(
        mgr.commit_native_mutation("op-hostile", "replace", "memory", sentinel, write)
    ))
    owner.start()
    assert entered.wait(timeout=2)
    follower.start()
    release.set()
    owner.join(timeout=2)
    follower.join(timeout=2)

    valid_failure = {"success": False, "operation_id": "op-hostile", "revision": 12,
                     "status": "conflict", "error": "native_mutation_failed"}
    expected = valid_failure if success is False else {
        "success": False, "operation_id": "op-hostile", "error": "native_mutation_failed",
    }
    results.append(mgr.commit_native_mutation("op-hostile", "replace", "memory", sentinel,
                   lambda: pytest.fail("cache must not call writer")))
    assert results == [expected, expected, expected]
    assert sentinel not in journal.path.read_text(encoding="utf-8")
    replay = MemoryManager(native_journal=NativeMutationJournal(tmp_path))
    assert replay.commit_native_mutation(
        "op-hostile", "replace", "memory", sentinel,
        lambda: pytest.fail("replay must not call writer"),
    ) == expected


def test_hostile_writer_exception_is_content_free_in_result_and_journal(tmp_path):
    sentinel = "HOSTILE MEMORY TEXT"
    journal = NativeMutationJournal(tmp_path)
    mgr = MemoryManager(native_journal=journal)

    def write():
        raise ValueError(sentinel)

    result = mgr.commit_native_mutation("op-error", "add", "memory", sentinel, write)
    assert result == {
        "success": False,
        "operation_id": "op-error",
        "error": "native_mutation_failed",
    }
    assert sentinel not in journal.path.read_text(encoding="utf-8")


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


def test_same_thread_reentry_returns_content_free_result(tmp_path):
    from agent.native_mutation_journal import NativeMutationJournal

    mgr = MemoryManager(native_journal=NativeMutationJournal(tmp_path))

    def write():
        return mgr.commit_native_mutation("op-reentrant", "add", "memory", "secret", write)

    result = mgr.commit_native_mutation("op-reentrant", "add", "memory", "secret", write)
    assert result["error"] == "native_operation_reentrant"
    assert "content" not in result
