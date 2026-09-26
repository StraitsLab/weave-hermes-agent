"""PB-F0: opt-in identity epoch (agent.identity_epoch_rebuild). Real SessionDB + AIAgent + native restore;
the flag-off golden was captured at the pin 2460a042 (.lane/capture_golden.py) and is compared byte-for-byte."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from agent.conversation_loop import _restore_or_build_system_prompt
from agent.prompt_builder import DEFAULT_AGENT_IDENTITY
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_state import SessionDB
from run_agent import AIAgent
from tests.agent.test_turn_context import _build

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "identity_epoch_flag_off_golden.json"
SID = "20260926_120000_golden"
HISTORY = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}]


def write_home(home: Path, soul: str = "SOUL-A-v1\n", flag: bool | None = True) -> Path:
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


def pin_clock_and_host(monkeypatch) -> None:  # also used by .lane/capture_golden.py
    import agent.prompt_builder as pb
    import hermes_time
    import run_agent

    monkeypatch.setattr(hermes_time, "now", lambda: dt.datetime(2026, 9, 26, 12, 0, tzinfo=dt.timezone.utc))
    for mod in (pb, run_agent):  # run_agent binds its own copy at import
        monkeypatch.setattr(mod, "build_environment_hints", lambda: "")


@pytest.fixture(autouse=True)
def _pinned(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    pin_clock_and_host(monkeypatch)


def _prologue(agent, history, **kw):  # the real turn prologue (agent/turn_context.build_turn_context)
    return _build(agent, conversation_history=history, restore_or_build_system_prompt=_restore_or_build_system_prompt, **kw)


def _stored(db, sid):
    return db.get_session(sid)["system_prompt"]


def _edit(home, text):
    (home / "SOUL.md").write_text(text, encoding="utf-8")


def test_continuing_session_reuses_unchanged_soul_and_adopts_new_soul_exactly_once(tmp_path):
    _, db = run_turn(home := write_home(tmp_path / "profiles" / "a"), SID, [])
    _, db_b = run_turn(sib := write_home(tmp_path / "profiles" / "b", "SOUL-B-v1\n"), "sb", [])
    assert "SOUL-A-v1" in (v1 := _stored(db, SID)) and "Identity epoch: " in v1
    for _ in range(2):  # unchanged SOUL: stored bytes reused, no rebuild (no cache break)
        assert (same := run_turn(home, SID, HISTORY, db)[0]).builds == [] and same._cached_system_prompt == v1 == _stored(db, SID)
    db.append_message(SID, "user", "hi")
    db.safe_fork_session(SID, "child")  # a forked child inherits the stamp ...
    assert _stored(db, "child") == v1
    _edit(home, "SOUL-A-v2\n")
    agent, db = run_turn(home, SID, HISTORY, db)
    assert agent.builds == [1] and agent._cached_system_prompt == (v2 := _stored(db, SID))
    assert "SOUL-A-v2" in v2 and "SOUL-A-v1" not in v2
    assert [m["content"] for m in db.get_messages(SID)] == ["hi"]  # same id, history kept
    assert (again := run_turn(home, SID, HISTORY, db)[0]).builds == [] and again._cached_system_prompt == v2
    assert run_turn(home, "child", HISTORY, db)[0].builds == [1] and "SOUL-A-v2" in _stored(db, "child")  # ... and rebuilds
    assert run_turn(sib, "sb", HISTORY, db_b)[0].builds == [] and "SOUL-B" not in v2  # sibling home untouched


def test_gateway_cached_agent_rebuilt_on_digest_change_and_reused_otherwise(tmp_path):
    from tests.gateway.test_api_server_mcp_reload import _make_turn_runner, _run_turn, _write_config

    _write_config(home := tmp_path / "gw")
    cfg = {**yaml.safe_load((home / "config.yaml").read_text()), "agent": {"identity_epoch_rebuild": True}, "mcp_servers": {}}
    (home / "config.yaml").write_text(yaml.safe_dump(cfg))
    _edit(home, "GW-SOUL-V1")
    db, runner, histories, captures = SessionDB(home / "state.db"), _make_turn_runner(), {}, []
    runner._session_db = type("DB", (), {"_db": db, "get_session": AsyncMock(side_effect=db.get_session)})()
    keys = runner._extract_cache_busting_config  # pin the process-global registry generation (bumped under load)
    runner._extract_cache_busting_config = lambda c: {**keys(c), "tools.registry_generation": 0}

    class Stub(AIAgent):  # real agent + real turn prologue; only inference is replaced
        def __init__(self, **kw):
            super().__init__(**{**kw, "skip_memory": True, "skip_context_files": True, "load_soul_identity": True})
            self._environment_probe, self.compression_enabled = False, False

        def run_conversation(self, message, **kw):
            ctx = _prologue(self, kw.get("conversation_history"), user_message=message)
            captures.append({"prompt": ctx.active_system_prompt})
            self._session_messages = ctx.messages + [{"role": "assistant", "content": "ok"}]
            return dict(final_response="ok", messages=self._session_messages, completed=True, api_calls=0)

    def turn(label):  # + the native post-turn message-count rebaseline
        out = _run_turn(runner, home, "r", label, Stub, captures, histories)
        return asyncio.run(runner._refresh_agent_cache_message_count("r", "mcp-reload-r")) or out

    try:
        first, same = turn("t1"), turn("t2")
        assert same["reused"] and same["capture"]["prompt"] == first["capture"]["prompt"]
        _edit(home, "GW-SOUL-V2")
        changed, stable = turn("t3"), turn("t4")
        assert not changed["reused"] and changed["agent"].session_id == first["agent"].session_id
        assert "GW-SOUL-V2" in changed["capture"]["prompt"] and "GW-SOUL-V1" not in changed["capture"]["prompt"]
        assert stable["reused"] and stable["capture"]["prompt"] == changed["capture"]["prompt"]
        assert [m["content"] for m in histories["r"] if m["role"] == "user"] == ["t1", "t2", "t3", "t4"]
    finally:
        db.close()


def test_reused_agent_adopts_new_soul_at_next_turn_boundary(tmp_path):  # same live CLI/native AIAgent
    agent, db = run_turn(home := write_home(tmp_path / "p1"), SID, [])
    agent.compression_enabled, agent.builds = False, []
    turn = lambda: _prologue(agent, HISTORY).active_system_prompt  # noqa: E731
    assert "SOUL-A-v1" in turn() and agent.builds == [] and _edit(home, "SOUL-A-v2\n") is None
    assert "SOUL-A-v2" in (v2 := turn()) and agent.builds == [1] and v2 == _stored(db, SID)
    assert turn() == v2 and agent.builds == [1]
    agent._identity_epoch_rebuild = False  # flag off: never re-checks
    _edit(home, "SOUL-A-v3\n")
    assert turn() == v2 and agent.builds == [1]


@pytest.mark.parametrize("pre,post,fail,held,final", [  # the stamp certifies the bytes the loader read
    (None, "SOUL-A-v2\n", False, "SOUL-A-v1", "SOUL-A-v2"),  # edit mid-turn, after the held turn's read
    ("SOUL-A-B\n", "SOUL-A-v1\n", False, "SOUL-A-B", "SOUL-A-v1"),  # A->B->A: loader saw B, disk rolled back
    ("SOUL-A-v2\n", None, True, DEFAULT_AGENT_IDENTITY, "SOUL-A-v2"),  # transient read OSError -> fallback
])
def test_held_turn_keeps_loaded_prompt_and_next_turn_converges(tmp_path, monkeypatch, pre, post, fail, held, final):
    import run_agent
    home, load, read, seen = write_home(tmp_path / "p1"), run_agent.load_soul_md, Path.read_bytes, []

    def read_once(p):  # fault the loader's OWN SOUL.md read: edit right after it, or NotADirectoryError
        if p.name != "SOUL.md" or seen.append(p) or len(seen) > 1:
            return read(p)
        return (read(p / "x" if fail else p), post and _edit(home, post))[0]

    def faulty(*a, **k):
        with monkeypatch.context() as m:
            return (pre and _edit(home, pre), m.setattr(Path, "read_bytes", read_once), load(*a, **k))[2]

    with monkeypatch.context() as m:
        m.setattr(run_agent, "load_soul_md", faulty)
        first, db = run_turn(home, SID, [])
    assert first._cached_system_prompt == _stored(db, SID) and held in _stored(db, SID) and final not in _stored(db, SID)
    later, db = run_turn(home, SID, HISTORY, db)
    assert later.builds == [1] and final in _stored(db, SID) and later._cached_system_prompt == _stored(db, SID)
    assert run_turn(home, SID, HISTORY, db)[0].builds == []  # converged: no rebuild loop


@pytest.mark.parametrize("label,flag", [("flag_absent", None), ("flag_false", False)])
def test_flag_off_prompt_and_signature_are_byte_identical_to_pin(tmp_path, label, flag):
    golden, home = json.loads(GOLDEN.read_text(encoding="utf-8")), write_home(tmp_path / "home", "GOLDEN-SOUL v1: pinned persona.\n", flag)
    _, db = run_turn(home, SID, [])
    assert normalize(_stored(db, SID), home) == golden["prompts"][label]
    assert signature() == golden["signature"]
    _edit(home, "SOUL-v2\n")  # flag off: a continuing session keeps its stored prompt
    agent, db = run_turn(home, SID, HISTORY, db)
    assert agent.builds == [] and normalize(agent._cached_system_prompt, home) == golden["prompts"][label]
