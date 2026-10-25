"""Persisted chat picks must reach native inference, not just session metadata."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
                    agent.model = value
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
    return [(p["key"], p["value"]) for m, p in native.calls if m == "config.set"]


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
        session["agent"].model = value
        return {"value": value, "warning": ""}

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
