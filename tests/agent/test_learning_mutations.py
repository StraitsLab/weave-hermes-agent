"""Behavior contracts for journey node edit/delete (agent.learning_mutations).

Exercises the real on-disk resolution (skills dir + MEMORY.md/USER.md chunking)
against a temp HERMES_HOME, never mocks — the id→file mapping is the whole point.
"""

from __future__ import annotations

import pytest

from agent import learning_mutations as lm
from hermes_constants import get_hermes_home

_SKILL = """---
name: my-skill
description: A test skill.
---

# My Skill

Body.
"""


@pytest.fixture
def home():
    base = get_hermes_home()
    (base / "memories").mkdir(parents=True, exist_ok=True)
    (base / "memories" / "MEMORY.md").write_text("alpha note\nline two\n§\nbeta note", encoding="utf-8")
    (base / "memories" / "USER.md").write_text("user profile note", encoding="utf-8")
    skill = base / "skills" / "my-skill"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(_SKILL, encoding="utf-8")
    return base


def test_parse_node_kind():
    assert lm.parse_node_kind("memory:memory:0") == "memory"
    assert lm.parse_node_kind("memory:profile:3") == "memory"
    assert lm.parse_node_kind("debugging-hermes") == "skill"


def test_memory_mutation_without_manager_is_rejected(home):
    result = lm.edit_node("memory:memory:0", "should not write")
    assert result == {"ok": False, "message": "native memory manager unavailable"}
    assert (home / "memories" / "MEMORY.md").read_text(encoding="utf-8").startswith("alpha note")


def test_skill_mutation_without_manager_is_preserved(home):
    result = lm.edit_node("my-skill", _SKILL.replace("Body.", "Updated."))
    assert result["ok"] is True
    assert "Updated." in (home / "skills" / "my-skill" / "SKILL.md").read_text(encoding="utf-8")








def test_edit_memory_replaces_chunk(home):
    from agent.memory_manager import MemoryManager

    manager = MemoryManager()
    try:
        result = lm.edit_node("memory:profile:2", "rewritten profile", memory_manager=manager, operation_id="edit-profile")
    finally:
        manager.shutdown_all()
    assert result["ok"]
    assert (home / "memories" / "USER.md").read_text(encoding="utf-8").strip() == "rewritten profile"








def test_skill_detail_returns_skill_md(home):
    d = lm.node_detail("my-skill")
    assert d["ok"] and d["kind"] == "skill"
    assert "name: my-skill" in d["content"]




def test_delete_pinned_skill_refused(home):
    from tools import skill_usage

    skill_usage.set_pinned("my-skill", True)
    res = lm.delete_node("my-skill")
    assert not res["ok"]
    assert "pinned" in res["message"]
    assert (home / "skills" / "my-skill").exists()






def test_memory_writes_match_memory_tool_format(home):
    """A journey mutation must leave the file byte-identical to what the memory
    tool itself writes — same §-join, no trailing-newline drift — so the two
    surfaces never fight over format and indices stay aligned."""
    from tools.memory_tool import ENTRY_DELIMITER, MemoryStore

    from agent.memory_manager import MemoryManager

    manager = MemoryManager()
    try:
        result = lm.edit_node("memory:memory:0", "alpha rewritten", memory_manager=manager, operation_id="edit-memory")
    finally:
        manager.shutdown_all()
    assert result["ok"]
    path = home / "memories" / "MEMORY.md"
    entries = MemoryStore._read_file(path)

    assert entries == ["alpha rewritten", "beta note"]
    assert path.read_text(encoding="utf-8") == ENTRY_DELIMITER.join(entries)
