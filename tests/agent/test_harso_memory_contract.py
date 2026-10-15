from agent.harso_memory_ids import new_operation_id
"""Harso native bridge uses upstream's committed patch output, not the span."""
import json
import uuid
import pytest
from tools.memory_tool import MemoryStore, memory_tool
from agent.memory_manager import MemoryManager


def test_tool_revision_and_explicit_target(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    store = MemoryStore()
    store.load_from_disk()
    from tools.memory_tool import MEMORY_SCHEMA
    assert "target" in MEMORY_SCHEMA["parameters"]["required"]
    assert not json.loads(memory_tool(action="replace", target="memory", old_text="x", content="y", store=store))["success"]
    add = json.loads(memory_tool(action="add", target="memory", content="Likes tea with oat milk", store=store))
    patch = json.loads(memory_tool(action="patch", target="memory", old_text="tea", content="coffee", store=store))
    assert type(add["revision"]) is int and patch["revision"] > add["revision"]
    assert patch["patched_entry_after"] == "Likes coffee with oat milk"


def test_native_bridge_sends_complete_patch_and_reports_ack(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    manager = MemoryManager()
    class Provider:
        name = "harso"
        def get_tool_schemas(self): return []
        def on_memory_write(self, action, target, content, *, metadata):
            assert (action, target, content) == ("patch", "memory", "Likes coffee with oat milk")
            assert metadata["old_text"] == "Likes tea with oat milk"
            assert type(metadata["revision"]) is int
            uuid.UUID(metadata["operation_id"])
    manager.add_provider(Provider())
    result = manager.notify_memory_tool_write(json.dumps({
        "success": True, "revision": 12,
        "patched_entry_before": "Likes tea with oat milk", "patched_entry_after": "Likes coffee with oat milk"}),
        {"action": "patch", "target": "memory", "old_text": "tea", "content": "coffee"},
        build_metadata=lambda: {"operation_id": new_operation_id()})
    assert result is True


def test_operation_id_fallback_on_older_python(monkeypatch):
    from agent.harso_memory_ids import new_operation_id
    monkeypatch.delattr(uuid, "uuid7", raising=False)
    first, second = uuid.UUID(new_operation_id()), uuid.UUID(new_operation_id())
    assert first.version == second.version == 7 and first != second
    assert first.int >> 80 > 1_700_000_000_000


def test_learning_uses_request_owned_manager(tmp_path, monkeypatch):
    from agent.learning_mutations import edit_node, delete_node
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path / "memories")
    store = MemoryStore()
    store.load_from_disk()
    memory_tool(action="add", target="memory", content="Likes tea", store=store)
    manager = MemoryManager()
    seen = []
    class Provider:
        name = "harso"
        def get_tool_schemas(self): return []
        def on_memory_write(self, action, target, content, *, metadata):
            seen.append((action, target, content, metadata))
    manager.add_provider(Provider())
    assert edit_node("memory:memory:0", "Likes coffee", memory_manager=manager, operation_id="edit-op")["ok"]
    assert delete_node("memory:memory:0", memory_manager=manager, operation_id="delete-op")["ok"]
    assert [(x[0], x[1], x[2]) for x in seen] == [("patch", "memory", "Likes coffee"), ("remove", "memory", "Likes coffee")]
    assert all(type(x[3]["revision"]) is int for x in seen)
