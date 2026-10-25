"""Persisted chat picks must reach native inference, not just session metadata."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway import native_bot_admission as admission
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB
from tui_gateway import server


@pytest.fixture
def native(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("chat", "api_server", model="default-model")
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "fixture"}))
    monkeypatch.setattr(adapter, "_ensure_session_db_async", AsyncMock(return_value=db))
    agent = SimpleNamespace(model="default-model", reasoning_config={"enabled": True, "effort": "low"},
                            service_tier=None)
    session = {"agent": agent, "session_key": "chat", "running": False}
    monkeypatch.setattr(server, "_sess", lambda *_: (session, None))
    monkeypatch.setattr(admission, "_answer_blocking_approval", lambda *_: False)
    calls = []

    class Transport:
        ref = None
        failure = None
        confirm = False
        noop = False

        def rpc(self, method, params):
            calls.append((method, params))
            if method == "session.resume":
                return {"session_id": "live-chat"}
            if method == "config.set":
                if self.failure == params["key"]:
                    raise RuntimeError("fixture config failure")
                if self.confirm:
                    return {"confirm_required": True}
                if self.noop:
                    return {}
                key, value = params["key"], params["value"]
                if key == "model":
                    from hermes_cli.model_switch import parse_model_switch_args
                    agent.model = parse_model_switch_args(value).model_input
                elif key == "reasoning":
                    agent.reasoning_config = {"enabled": True, "effort": value}
                elif key == "fast":
                    agent.service_tier = "priority" if value == "on" else None
                return {}
            assert method == "prompt.submit"
            return {"status": "streaming"}

    transport = Transport()
    adapter._native_bot_transports = {("default", "chat"): transport}
    yield SimpleNamespace(db=db, adapter=adapter, agent=agent, session=session,
                          transport=transport, calls=calls)
    db.close()


def submit(native, ref="ref"):
    return asyncio.run(admission.admit(native.adapter, "default", "chat", "hello", ref, ref))


def lock(native, model="picked-model", effort="high", fast=True):
    native.db.update_session_runtime_lock("chat", model=model, confirmed=True,
        model_options={"reasoning": {"effort": effort}, "fast": fast})


def settings(native):
    from hermes_cli.model_switch import parse_model_switch_args
    return [(p["key"], parse_model_switch_args(p["value"]).model_input
             if p["key"] == "model" else p["value"])
            for m, p in native.calls if m == "config.set"]


def test_lock_applied_before_prompt_on_every_turn(native):
    lock(native)
    assert submit(native) == "streaming"
    assert settings(native) == [("model", "picked-model"), ("reasoning", "high"), ("fast", "on")]
    assert [m for m, _ in native.calls] == ["session.resume", "config.set", "config.set", "config.set", "prompt.submit"]
    assert all(p["session_id"] == "live-chat" for m, p in native.calls if m == "config.set")
    native.calls.clear()
    lock(native, model="next-model", effort="medium", fast=False)
    assert submit(native, "next") == "streaming"
    assert settings(native) == [("model", "next-model"), ("reasoning", "medium"), ("fast", "off")]
    assert native.agent.service_tier is None


def test_no_lock_or_matching_lock_does_not_switch(native):
    assert submit(native) == "streaming"
    assert settings(native) == []
    lock(native, model=native.agent.model, effort="low", fast=False)
    native.calls.clear()
    assert submit(native, "next") == "streaming"
    assert settings(native) == []


@pytest.mark.parametrize("key", ["model", "reasoning", "fast"])
def test_apply_error_fails_before_prompt_or_admission_hold(native, key):
    lock(native)
    native.transport.failure = key
    with pytest.raises(RuntimeError, match="fixture config failure"):
        submit(native)
    assert not any(m == "prompt.submit" for m, _ in native.calls)
    assert native.adapter._native_submit_active_refs == {}


@pytest.mark.parametrize("mode", ["confirm", "noop"])
def test_unapplied_lock_fails_closed(native, mode):
    lock(native)
    setattr(native.transport, mode, True)
    with pytest.raises(RuntimeError, match="model lock"):
        submit(native)
    assert not any(m == "prompt.submit" for m, _ in native.calls)


def test_matching_effort_still_enables_reasoning(native):
    lock(native, model=native.agent.model, effort="low", fast=False)
    native.agent.reasoning_config["enabled"] = False
    assert submit(native) == "streaming"
    assert native.agent.reasoning_config["enabled"] is True


def test_busy_turn_is_not_reconfigured(native):
    lock(native)
    native.session["running"] = True
    with pytest.raises(RuntimeError, match="native bot is busy"):
        submit(native)
    assert settings(native) == []


def test_http_model_lock_reaches_native_inference(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(root / "tests/gateway/native_bot_smoke.py"), str(root),
         "--python", sys.executable, "--model-lock-only"],
        env=os.environ | {"TMPDIR": str(tmp_path)}, text=True, capture_output=True, timeout=100,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"result": "passed"' in result.stdout


def test_native_lock_drives_real_tui_config_set(native, monkeypatch):
    """Only the provider switch boundary is stubbed; use the stock RPC handlers."""
    native.agent.request_overrides = {}
    native.agent._session_db = native.db
    monkeypatch.setitem(server._sessions, "live-chat", native.session)
    monkeypatch.setattr(server, "_emit", lambda *_: None)

    def switch(sid, session, value, **kwargs):
        assert sid == "live-chat" and session is native.session
        from hermes_cli.model_switch import parse_model_switch_args
        session["agent"].model = parse_model_switch_args(value).model_input
        return {"value": session["agent"].model, "warning": ""}

    monkeypatch.setattr(server, "_apply_model_switch", switch)
    fake_rpc = native.transport.rpc

    def rpc(method, params):
        if method == "config.set":
            native.calls.append((method, params))
            response = server._methods[method]("fixture", params)
            assert "error" not in response, response
            return response["result"]
        if method == "prompt.submit":
            assert native.agent.model == "gpt-6"
            assert native.agent.reasoning_config == {"enabled": True, "effort": "high"}
            assert native.agent.service_tier == "priority"
        return fake_rpc(method, params)

    monkeypatch.setattr(native.transport, "rpc", rpc)
    lock(native, model="gpt-6")
    assert submit(native) == "streaming"
    assert native.session["create_service_tier_override"] == "priority"
    assert native.db.get_session("chat")["model"] == "gpt-6"


def test_lock_read_does_not_load_session_or_flush_usage(native, monkeypatch):
    lock(native)
    monkeypatch.setattr(native.db, "get_session", Mock(side_effect=AssertionError("full row read")))
    flush = Mock(side_effect=AssertionError("usage flush"))
    monkeypatch.setattr(native.db, "flush_token_counts", flush)
    assert submit(native) == "streaming"
    flush.assert_not_called()


def test_model_config_reader_is_narrow_and_does_not_flush(native, monkeypatch):
    import json

    lock(native)
    monkeypatch.setattr(native.db, "flush_token_counts", Mock(side_effect=AssertionError("usage flush")))
    row = native.db.get_session_model_config("chat")
    assert set(row) == {"model_config"}
    assert json.loads(row["model_config"])["browser_model_lock"]["model"] == "picked-model"
    assert native.db.get_session_model_config("missing") is None


@pytest.fixture
def stock_switch(native, monkeypatch, tmp_path):
    """Use real config.set/parser/switch_model; stub only I/O and agent execution."""
    import hermes_cli.model_switch as ms
    import yaml

    cfg = {"model": {"default": "default-model", "provider": "custom",
                     "base_url": "https://cell.example/v1", "persist_switch_by_default": True}}
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **kw: cfg)
    # Single catalog view: v12 providers otherwise appear twice under legacy slugs.
    monkeypatch.setattr("hermes_cli.config.get_compatible_custom_providers", lambda *_: [])
    monkeypatch.setattr(ms, "DIRECT_ALIASES", {})
    monkeypatch.setattr(ms, "list_provider_models", lambda *_: [])
    monkeypatch.setattr(ms, "get_model_info", lambda *a, **kw: None)
    monkeypatch.setattr(ms, "get_model_capabilities", lambda *a, **kw: None)
    monkeypatch.setattr("hermes_cli.models.detect_provider_for_model", lambda *_: None)
    monkeypatch.setattr("hermes_cli.models.validate_requested_model", lambda *a, **kw:
                        {"accepted": True, "persist": True, "recognized": True, "message": ""})
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda **kw:
                        {"provider": kw.get("requested"), "base_url": cfg["model"]["base_url"],
                         "api_key": "fixture", "api_mode": "chat_completions"})
    native.agent.provider = "custom"
    native.agent.base_url = cfg["model"]["base_url"]
    native.agent.api_key = "fixture"
    native.agent.request_overrides = {}
    swaps = []

    def switch_model(**kw):
        swaps.append(kw)
        native.agent.model = kw["new_model"]
        native.agent.provider = kw["new_provider"]
        native.agent.base_url = kw["base_url"]

    native.agent.switch_model = switch_model
    monkeypatch.setitem(server._sessions, "live-chat", native.session)
    monkeypatch.setattr(server, "_restart_slash_worker", lambda *_: None)
    monkeypatch.setattr(server, "_persist_live_session_runtime", lambda *_: None)
    monkeypatch.setattr(server, "_persist_live_session_system_prompt", lambda *_: None)
    monkeypatch.setattr(server, "_append_model_switch_marker", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_session_info", lambda *_: {})
    monkeypatch.setattr(server, "_emit", lambda *_: None)
    persist = Mock()
    monkeypatch.setattr(server, "_persist_model_switch", persist)
    fake_rpc = native.transport.rpc

    def rpc(method, params):
        if method == "config.set":
            native.calls.append((method, params))
            response = server._methods[method]("fixture", params)
            assert "error" not in response, response
            return response["result"]
        return fake_rpc(method, params)

    monkeypatch.setattr(native.transport, "rpc", rpc)
    return SimpleNamespace(config=cfg, path=config_path, persist=persist, swaps=swaps)


def test_native_model_lock_is_session_only_even_with_persist_default(native, stock_switch, monkeypatch):
    other_agent = SimpleNamespace(model="default-model", provider="custom", base_url="https://cell.example/v1")
    other = {"agent": other_agent, "session_key": "other"}
    monkeypatch.setitem(server._sessions, "other", other)
    before = stock_switch.path.read_bytes()
    lock(native, effort=None, fast=False)
    assert submit(native) == "streaming"
    stock_switch.persist.assert_not_called()
    assert stock_switch.path.read_bytes() == before
    assert vars(other_agent) == {"model": "default-model", "provider": "custom", "base_url": "https://cell.example/v1"}
    assert "model_override" not in other


@pytest.mark.parametrize("route", ["configured-provider", "direct-alias"])
def test_native_model_lock_rejects_and_reverts_provider_reroute(native, stock_switch, route):
    if route == "configured-provider":
        stock_switch.config["providers"] = {"competitor": {
            "base_url": "https://competitor.example/v1", "api_key": "fixture", "models": ["picked-model"]}}
    else:
        # Same provider label, different URL: checking provider alone is insufficient.
        stock_switch.config["model_aliases"] = {"pick": {
            "model": "picked-model", "provider": "custom",
            "base_url": "https://alias.example/v1", "api_key": "fixture"}}
    lock(native, effort=None, fast=False)
    with pytest.raises(RuntimeError, match="model lock"):
        submit(native)
    assert len(stock_switch.swaps) == 2
    assert native.agent.model == "default-model"
    assert native.agent.provider == "custom"
    assert native.agent.base_url == "https://cell.example/v1"
    stock_switch.persist.assert_not_called()
    assert not any(m == "prompt.submit" for m, _ in native.calls)
    assert native.adapter._native_submit_active_refs == {}
