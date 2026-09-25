"""PB-F0: opt-in identity epoch (agent.identity_epoch_rebuild). Real SessionDB + AIAgent + native restore;
the flag-off golden was captured at the pin 2460a042 (.lane/capture_golden.py) and is compared byte-for-byte."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pytest

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "identity_epoch_flag_off_golden.json"
SID = "20260926_120000_golden"
HISTORY = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}]


def pin_clock_and_host(monkeypatch) -> None:
    import agent.prompt_builder as pb
    import hermes_time

    monkeypatch.setattr(hermes_time, "now", lambda: dt.datetime(2026, 9, 26, 12, 0, tzinfo=dt.timezone.utc))
    monkeypatch.setattr(pb, "build_environment_hints", lambda: "")


def write_home(home: Path, soul: str, flag: bool | None = None) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    (home / "SOUL.md").write_text(soul, encoding="utf-8")
    if flag is not None:
        (home / "config.yaml").write_text(f"agent:\n  identity_epoch_rebuild: {str(flag).lower()}\n", encoding="utf-8")
    return home


def normalize(prompt: str, home: Path) -> str:
    out, root = prompt.replace(str(home), "<HOME>"), os.environ.get("HERMES_HOME", "")
    return out.replace(root, "<ROOT>") if root else out


def run_turn(home: Path, session_id: str, history: list, db=None):
    """One turn start on ``home`` as gateway/run.py builds it (load_soul_identity)."""
    from agent.conversation_loop import _restore_or_build_system_prompt
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_state import SessionDB
    from run_agent import AIAgent

    token = set_hermes_home_override(str(home))
    try:
        db = db or SessionDB(home / "state.db")
        if db.get_session(session_id) is None:
            db.create_session(session_id, "api_server")
        agent = AIAgent(model="golden/model", api_key="k", base_url="http://127.0.0.1:1/v1", provider="custom",
                        session_id=session_id, session_db=db, platform="", quiet_mode=True, skip_memory=True,
                        skip_context_files=True, load_soul_identity=True, enabled_toolsets=[])
        agent._environment_probe = False
        build, agent.builds = agent._build_system_prompt, []
        agent._build_system_prompt = lambda *a, **k: agent.builds.append(1) or build(*a, **k)
        _restore_or_build_system_prompt(agent, None, history)
        return agent, db
    finally:
        reset_hermes_home_override(token)


def signature(**extra) -> str:
    from gateway.run import GatewayRunner

    rt = {"api_key": "k", "base_url": "http://127.0.0.1:1/v1", "provider": "custom", "api_mode": "chat_completions"}
    return GatewayRunner._agent_config_signature(
        "golden/model", rt, ["hermes-api-server"], "ephemeral", cache_keys={"compression.threshold": 0.5},
        user_id="u1", user_id_alt="u1-alt", skip_context_files=True, **extra)


@pytest.fixture(autouse=True)
def _pinned(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    pin_clock_and_host(monkeypatch)


def _stored(db, sid):
    return db.get_session(sid)["system_prompt"]


def _edit(home, text):
    (home / "SOUL.md").write_text(text, encoding="utf-8")


def test_continuing_session_adopts_new_soul_exactly_once(tmp_path):
    home = write_home(tmp_path / "p1", "SOUL-A-v1\n", flag=True)
    _, db = run_turn(home, SID, [])
    assert "SOUL-A-v1" in _stored(db, SID) and "Identity epoch: " in _stored(db, SID)
    db.append_message(SID, "user", "hi")
    _edit(home, "SOUL-A-v2\n")
    agent, db = run_turn(home, SID, HISTORY, db)
    v2 = _stored(db, SID)
    assert agent.builds == [1] and agent._cached_system_prompt == v2
    assert "SOUL-A-v2" in v2 and "SOUL-A-v1" not in v2
    assert [m["content"] for m in db.get_messages(SID)] == ["hi"]  # same id, history kept
    again, db = run_turn(home, SID, HISTORY, db)
    assert again.builds == [] and again._cached_system_prompt == v2


def test_unchanged_soul_reuses_stored_bytes(tmp_path):
    home = write_home(tmp_path / "p1", "SOUL-A-v1\n", flag=True)
    _, db = run_turn(home, SID, [])
    v1 = _stored(db, SID)
    for _ in range(2):
        agent, db = run_turn(home, SID, HISTORY, db)
        assert agent.builds == [] and agent._cached_system_prompt == v1 == _stored(db, SID)


def test_held_running_turn_keeps_its_prompt(tmp_path):
    home = write_home(tmp_path / "p1", "SOUL-A-v1\n", flag=True)
    held, db = run_turn(home, SID, [])
    before = held._cached_system_prompt
    _edit(home, "SOUL-A-v2\n")  # a running turn never re-enters restore (only while its prompt is None)
    assert held._cached_system_prompt == before and _stored(db, SID) == before
    assert "SOUL-A-v2" in run_turn(home, SID, HISTORY, db)[0]._cached_system_prompt


def test_sibling_profile_home_unaffected(tmp_path):
    a = write_home(tmp_path / "profiles" / "a", "SOUL-A-v1\n", flag=True)
    b = write_home(tmp_path / "profiles" / "b", "SOUL-B-v1\n", flag=True)
    (_, db_a), (_, db_b) = run_turn(a, "sa", []), run_turn(b, "sb", [])
    b_v1 = _stored(db_b, "sb")
    _edit(a, "SOUL-A-v2\n")
    agent_a, agent_b = run_turn(a, "sa", HISTORY, db_a)[0], run_turn(b, "sb", HISTORY, db_b)[0]
    assert "SOUL-A-v2" in agent_a._cached_system_prompt and "SOUL-B" not in agent_a._cached_system_prompt
    assert agent_b.builds == [] and agent_b._cached_system_prompt == b_v1


def test_forked_child_inherits_stamp_and_rebuilds_on_first_turn(tmp_path):
    home = write_home(tmp_path / "p1", "SOUL-A-v1\n", flag=True)
    _, db = run_turn(home, SID, [])
    db.append_message(SID, "user", "hi")
    db.safe_fork_session(SID, "child")
    assert _stored(db, "child") == _stored(db, SID)  # stamp inherited
    _edit(home, "SOUL-A-v2\n")
    child, db = run_turn(home, "child", HISTORY, db)
    assert child.builds == [1] and "SOUL-A-v2" in _stored(db, "child")


def test_gateway_cached_agent_rebuilt_on_digest_change_and_reused_otherwise(tmp_path):
    from gateway.run import GatewayRunner
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home = write_home(tmp_path / "p1", "SOUL-A-v1\n", flag=True)
    on = {"agent": {"identity_epoch_rebuild": True}}
    token = set_hermes_home_override(str(home))
    try:
        assert GatewayRunner._identity_epoch_digest({}) == ""
        assert GatewayRunner._identity_epoch_digest({"agent": {"identity_epoch_rebuild": False}}) == ""
        s1 = signature(identity_digest=GatewayRunner._identity_epoch_digest(on))
        assert s1 == signature(identity_digest=GatewayRunner._identity_epoch_digest(on))  # reused
        _edit(home, "SOUL-A-v2\n")
        assert s1 != signature(identity_digest=GatewayRunner._identity_epoch_digest(on))  # rebuilt
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize("label,flag", [("flag_absent", None), ("flag_false", False)])
def test_flag_off_prompt_and_signature_are_byte_identical_to_pin(tmp_path, label, flag):
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    home = write_home(tmp_path / "home", "GOLDEN-SOUL v1: pinned persona.\n", flag)
    _, db = run_turn(home, SID, [])
    assert normalize(_stored(db, SID), home) == golden["prompts"][label]
    assert signature() == golden["signature"]
    _edit(home, "SOUL-v2\n")  # flag off: a continuing session keeps its stored prompt
    agent, db = run_turn(home, SID, HISTORY, db)
    assert agent.builds == [] and normalize(agent._cached_system_prompt, home) == golden["prompts"][label]
