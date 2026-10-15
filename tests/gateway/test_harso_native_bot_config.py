"""Cells switch native bot sessions on in config.yaml, not only the environment (v0.21 read config)."""
from types import SimpleNamespace

import pytest

from gateway import native_bot_admission, native_session_credentials
from gateway.config import Platform
from gateway.session import SessionSource


@pytest.fixture
def config(monkeypatch):
    values = {}
    monkeypatch.delenv("HERMES_NATIVE_BOT_SESSIONS", raising=False)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: values)
    return values


def test_config_switch_enables_native_bot_sessions(config):
    assert native_bot_admission.enabled() is False
    config["gateway"] = {"native_bot_sessions": True}
    assert native_bot_admission.enabled() is True


def test_env_switch_still_enables(config, monkeypatch):
    monkeypatch.setenv("HERMES_NATIVE_BOT_SESSIONS", "1")
    assert native_bot_admission.enabled() is True


def _runner():
    store = SimpleNamespace(_generate_session_key=lambda source: "k", _entries={})
    return SimpleNamespace(session_store=store, _peek_session_state=lambda key: None,
                           _session_credential_lock=object(), session_credential_for=lambda *a: None)


def test_native_cell_without_bound_credential_uses_its_own_provider(config):
    config["gateway"] = {"native_bot_sessions": True}
    source = SessionSource(platform=Platform.API_SERVER, chat_id="s", chat_type="dm")
    user = {"model": {"provider": "custom:weave-gate-b", "default": "weave-chat"}}
    assert native_session_credentials.runtime(_runner(), source, None, user) is None


def test_gate_b_without_native_sessions_still_requires_a_bound_credential(config):
    source = SessionSource(platform=Platform.API_SERVER, chat_id="s", chat_type="dm")
    user = {"model": {"provider": "custom:weave-gate-b", "default": "weave-chat"}}
    with pytest.raises(RuntimeError, match="credential unavailable"):
        native_session_credentials.runtime(_runner(), source, None, user)
