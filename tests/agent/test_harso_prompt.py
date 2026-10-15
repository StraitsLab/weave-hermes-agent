"""Port of the Harso white-label prompt tests against v0.22 assembly."""
from agent.system_prompt import build_system_prompt
from tests.agent.test_system_prompt import _make_agent


def test_neutral_identity_reads_profile_setting(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"agent": {"engine_identity": False}})
    monkeypatch.setattr("agent.prompt_builder.load_soul_md", lambda *a, **k: None)
    agent = _make_agent(load_soul_identity=True, skip_context_files=True, valid_tool_names=set(),
                        model="private-model", provider="private-provider")
    prompt = build_system_prompt(agent)
    assert "You are a helpful AI assistant." in prompt
    assert "built by Nous" not in prompt
    assert "private-model" not in prompt
    assert "private-provider" not in prompt
    assert "docs.hermes" not in prompt


def test_default_identity_unchanged_and_soul_has_priority(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", dict)
    monkeypatch.setattr("agent.prompt_builder.load_soul_md", lambda *a, **k: None)
    agent = _make_agent(load_soul_identity=True, skip_context_files=True)
    assert build_system_prompt(agent) == build_system_prompt(_make_agent(
        load_soul_identity=True, skip_context_files=True, _engine_identity=True))
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"agent": {"engine_identity": False}})
    monkeypatch.setattr("agent.prompt_builder.load_soul_md", lambda *a, **k: "You are Harso.")
    assert "You are Harso." in build_system_prompt(agent)
