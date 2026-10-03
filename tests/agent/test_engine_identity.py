"""Exercise engine identity through real prompt assembly and Codex fallback."""

from datetime import datetime
from types import SimpleNamespace
from tempfile import TemporaryDirectory

import pytest

from agent.codex_responses_adapter import _preflight_codex_api_kwargs
from agent.system_prompt import build_system_prompt
from hermes_constants import get_hermes_home


@pytest.fixture
def prompt_home(monkeypatch):
    workspace = TemporaryDirectory(prefix="prompt-", dir="/tmp")
    monkeypatch.chdir(workspace.name)
    monkeypatch.setenv("TERMINAL_CWD", workspace.name)
    # Loading identity must not seed a default SOUL: this proof exercises
    # the documented no-SOUL fallback, not user-provided identity text.
    monkeypatch.setattr("hermes_cli.config.ensure_hermes_home", lambda: None)
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("HERMES_PLATFORM", "api_server")
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    skills = get_hermes_home() / "skills"
    for name, description in (
        ("hermes-agent", "Configure Hermes Agent by Nous Research."),
        ("writing", "Write clear sentences."),
    ):
        directory = skills / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n"
        )
    yield get_hermes_home()
    workspace.cleanup()


@pytest.mark.parametrize("engine_identity", [None, True, False])
def test_agent_reads_engine_identity_setting(prompt_home, engine_identity):
    from hermes_cli.config import load_config
    from run_agent import AIAgent

    if engine_identity is not None:
        (prompt_home / "config.yaml").write_text(
            f"agent:\n  engine_identity: {str(engine_identity).lower()}\n"
        )
    config = load_config()
    assert config["agent"]["engine_identity"] is (
        True if engine_identity is None else engine_identity
    )
    agent = AIAgent(
        model="claude-opus-x",
        provider="custom",
        api_key="test-only",
        base_url="http://127.0.0.1:1/v1",
        enabled_toolsets=[],
        quiet_mode=True,
        skip_memory=True,
        skip_context_files=True,
    )
    assert agent._engine_identity is (True if engine_identity is None else engine_identity)


def _make_agent(config=None, *, skills=True):
    agent = SimpleNamespace(
        load_soul_identity=True,
        skip_context_files=True,
        valid_tool_names={"skills_list", "skill_view"} if skills else set(),
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        model="claude-opus-x",
        provider="alibaba",
        platform="api_server",
        pass_session_id=True,
        session_id="20260101_120000_abc123",
        session_start=datetime(2026, 1, 1, 12, 0),
        _emit_status=lambda *_args, **_kwargs: None,
    )
    section = (config or {}).get("agent", {})
    if "engine_identity" in section:
        agent._engine_identity = section["engine_identity"]
    return agent


@pytest.mark.parametrize("skills", [True, False])
@pytest.mark.parametrize("backend", ["local", "docker"])
def test_false_removes_engine_model_and_provider(prompt_home, monkeypatch, skills, backend):
    monkeypatch.setenv("TERMINAL_ENV", backend)
    monkeypatch.setattr(
        "agent.prompt_builder._probe_remote_backend",
        lambda _backend: "OS: Linux\nShell: /bin/bash",
    )
    prompt = build_system_prompt(
        _make_agent({"agent": {"engine_identity": False}}, skills=skills)
    )
    for identity in ("hermes", "nous", "claude-opus-x", "alibaba"):
        assert identity not in prompt.lower(), f"Leaked {identity}: {prompt}"
    assert "You are a helpful AI assistant." in prompt
    assert "Conversation started:" in prompt
    assert "Session ID: 20260101_120000_abc123" in prompt
    assert "Platform: api_server" in prompt
    if skills:
        assert "writing: Write clear sentences." in prompt


@pytest.mark.parametrize("skills", [True, False])
def test_true_is_byte_identical_to_missing_setting(prompt_home, skills):
    before = build_system_prompt(_make_agent(skills=skills))
    enabled = build_system_prompt(
        _make_agent({"agent": {"engine_identity": True}}, skills=skills)
    )
    assert enabled.encode() == before.encode()
    assert "Hermes Agent" in before
    assert "Nous Research" in before
    assert "Model: claude-opus-x" in before
    assert "Provider: alibaba" in before
    if skills:
        assert "hermes-agent: Configure Hermes Agent" in before


@pytest.mark.parametrize("instructions", [None, "", "  "])
@pytest.mark.parametrize("engine_identity", [False, True])
def test_codex_blank_instructions_use_configured_identity(
    monkeypatch, engine_identity, instructions
):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"agent": {"engine_identity": engine_identity}},
    )
    request = _preflight_codex_api_kwargs(
        {"model": "claude-opus-x", "instructions": instructions, "input": []}
    )
    if engine_identity:
        from agent.prompt_builder import DEFAULT_AGENT_IDENTITY

        assert request["instructions"] == DEFAULT_AGENT_IDENTITY
    else:
        assert request["instructions"] == "You are a helpful AI assistant."


def test_codex_preserves_supplied_instructions(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"agent": {"engine_identity": False}},
    )
    request = _preflight_codex_api_kwargs(
        {"model": "claude-opus-x", "instructions": "You are Harso.", "input": []}
    )
    assert request["instructions"] == "You are Harso."
