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
from hermes_cli.config import get_compatible_custom_providers as REAL_CUSTOM_PROVIDERS
from hermes_cli.runtime_provider import resolve_runtime_provider as REAL_RESOLVE_RUNTIME


REAL_PERSIST_RUNTIME = server._persist_live_session_runtime


@pytest.fixture
def native(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("chat", "api_server", model="default-model")
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "fixture"}))
    monkeypatch.setattr(adapter, "_ensure_session_db_async", AsyncMock(return_value=db))
    cfg = {"model": {"default": "default-model", "provider": "custom",
                     "base_url": "https://cell.example/v1"}}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **kw: cfg)
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda **kw: {
        "provider": kw.get("requested") or "custom", "base_url": cfg["model"]["base_url"],
        "api_key": "fixture", "api_mode": "chat_completions"})
    agent = SimpleNamespace(model="default-model", provider="custom", base_url="https://cell.example/v1",
                            reasoning_config={"enabled": True, "effort": "low"}, service_tier=None)
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
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda **kw: {
        "provider": kw.get("requested") or "custom",
        "base_url": (cfg.get("providers", {}).get(kw.get("requested"), {}).get("base_url")
                     or cfg["model"]["base_url"]),
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
    assert native.agent.model == "picked-model"
    assert native.agent.provider == "custom"
    assert native.agent.base_url == (
        "https://cell.example/v1" if route == "configured-provider" else "https://alias.example/v1")
    assert native.session.get("_native_model_lock_unusable") is (route == "direct-alias")
    persisted = native.db.get_session_model_config("chat")["model_config"]
    import json
    assert json.loads(persisted)["base_url"] == "https://cell.example/v1"
    stock_switch.persist.assert_not_called()
    assert not any(m == "prompt.submit" for m, _ in native.calls)
    assert native.adapter._native_submit_active_refs == {}


@pytest.mark.parametrize("failure", ["error", "noop", "confirm", "wrong-model", "wrong-route"])
def test_failed_rollback_self_heals_before_retry(native, stock_switch, monkeypatch, failure):
    """Real catalog reroute, failed restore, then retry (even without a lock)."""
    stock_switch.config["providers"] = {"competitor": {
        "base_url": "https://competitor.example/v1", "api_key": "fixture", "models": ["picked-model"]}}
    real_rpc = native.transport.rpc
    attempts = 0

    def rpc(method, params):
        nonlocal attempts
        if method == "config.set" and params["key"] == "model":
            attempts += 1
            if attempts == 2:
                assert "--session" in params["value"]
                if failure == "error":
                    raise RuntimeError("rollback failed")
                if failure == "confirm":
                    return {"confirm_required": True}
                if failure == "wrong-model":
                    native.agent.provider = "custom"
                    native.agent.base_url = "https://cell.example/v1"
                    native.agent.model = "default-model"
                elif failure == "wrong-route":
                    native.agent.model = "picked-model"
                return {}
        return real_rpc(method, params)

    monkeypatch.setattr(native.transport, "rpc", rpc)
    lock(native, effort=None, fast=False)
    with pytest.raises(RuntimeError):
        submit(native)
    assert not any(m == "prompt.submit" for m, _ in native.calls)
    assert submit(native, "retry") == "streaming"
    assert (native.agent.model, native.agent.provider, native.agent.base_url) == (
        "picked-model", "custom", "https://cell.example/v1")
    # Removing the lock still heals back to the profile default, not the poison.
    native.db.patch_session_model_config("chat", {"browser_model_lock": None})
    native.agent.base_url = "https://competitor.example/v1"
    native.calls.clear()
    assert submit(native, "retry-without-lock") == "streaming"
    assert (native.agent.model, native.agent.provider, native.agent.base_url) == (
        "default-model", "custom", "https://cell.example/v1")
    stock_switch.persist.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("provider", "competitor"), ("base_url", "https://competitor.example/v1")])
@pytest.mark.parametrize("initial_lock", [False, True])
def test_matching_model_still_checks_original_session_route(native, stock_switch, field, value, initial_lock):
    if initial_lock:
        lock(native, effort=None, fast=False)
    assert submit(native) == "streaming"
    native.calls.clear()
    # A matching ID on the wrong provider/endpoint is not the same runtime.
    lock(native, model=native.agent.model, effort=None, fast=False)
    setattr(native.agent, field, value)
    assert submit(native, "retry") == "streaming"
    assert (native.agent.provider, native.agent.base_url) == ("custom", "https://cell.example/v1")
    assert settings(native) == [("model", native.agent.model)]


@pytest.mark.parametrize("has_lock", [False, True])
def test_first_admission_does_not_trust_already_switched_agent(native, stock_switch, has_lock):
    """Reviewer first-admission probe, with the safety assertion reversed."""
    native.agent.model = "picked-model"
    native.agent.provider = "competitor"
    native.agent.base_url = "https://competitor.example/v1"
    if has_lock:
        lock(native, effort=None, fast=False)
    assert submit(native) == "streaming"
    assert (native.agent.model, native.agent.provider, native.agent.base_url) == (
        "picked-model" if has_lock else "default-model", "custom", "https://cell.example/v1")


@pytest.mark.parametrize("has_lock", [False, True])
@pytest.mark.parametrize("failure", ["error", "noop"])
def test_rejected_route_is_not_accepted_after_cold_resume(native, stock_switch, monkeypatch, has_lock, failure):
    """Reviewer restart probe: enable real persistence and rebuild a fresh dict."""
    import json

    stock_switch.config["providers"] = {"competitor": {
        "base_url": "https://competitor.example/v1", "api_key": "fixture", "models": ["picked-model"]}}
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda **kw: {
        "provider": kw.get("requested") or "custom",
        "base_url": ("https://competitor.example/v1" if kw.get("requested") == "competitor"
                     else "https://cell.example/v1"),
        "api_key": "fixture", "api_mode": "chat_completions"})
    native.agent._session_db = native.db
    monkeypatch.setattr(server, "_persist_live_session_runtime", REAL_PERSIST_RUNTIME)
    real_rpc = native.transport.rpc
    attempts = 0

    def rpc(method, params):
        nonlocal attempts
        if method == "config.set" and params["key"] == "model":
            attempts += 1
            if attempts == 2:
                if failure == "error":
                    raise RuntimeError("rollback failed")
                return {}
        return real_rpc(method, params)

    monkeypatch.setattr(native.transport, "rpc", rpc)
    lock(native, effort=None, fast=False)
    with pytest.raises(RuntimeError):
        submit(native)
    assert not any(m == "prompt.submit" for m, _ in native.calls)
    persisted = json.loads(native.db.get_session("chat")["model_config"])
    assert persisted["base_url"] == "https://cell.example/v1"
    assert persisted["provider"] == "custom"
    # Also defend against an older process's already-poisoned durable row.
    native.db.patch_session_model_config("chat", {
        "provider": "competitor", "base_url": "https://competitor.example/v1"})
    if not has_lock:
        native.db.patch_session_model_config("chat", {"browser_model_lock": None})
    restored = server._stored_session_runtime_overrides(native.db.get_session("chat"))
    mo = restored["model_override"]
    cold_agent = SimpleNamespace(**vars(native.agent))
    cold_agent.model, cold_agent.provider, cold_agent.base_url = mo["model"], mo["provider"], mo["base_url"]
    cold = {"agent": cold_agent, "session_key": "chat", "running": False}
    monkeypatch.setattr(server, "_sess", lambda *_: (cold, None))
    monkeypatch.setitem(server._sessions, "live-chat", cold)
    native.agent = cold_agent
    # The real fixture switch must mutate the newly resumed agent too.
    cold_agent.switch_model = lambda **kw: vars(cold_agent).update(
        model=kw["new_model"], provider=kw["new_provider"], base_url=kw["base_url"])
    native.calls.clear()
    assert submit(native, "after-restart") == "streaming"
    assert (cold_agent.model, cold_agent.provider, cold_agent.base_url) == (
        "picked-model" if has_lock else "default-model", "custom", "https://cell.example/v1")
    persisted = json.loads(native.db.get_session("chat")["model_config"])
    assert persisted["base_url"] == "https://cell.example/v1"
    assert persisted["provider"] == "custom"


@pytest.mark.parametrize("provider", ["custom", "custom:weave-gate-b"])
def test_custom_config_route_matches_startup_and_stock_switch(native, stock_switch, monkeypatch, provider):
    import hermes_cli.runtime_provider as rp

    cfg = stock_switch.config
    cfg["model"].update(provider=provider, base_url="https://models-dev.harso.dev/v1")
    cfg["providers"] = {"weave-gate-b": {
        "base_url": "https://models-dev.harso.dev/v1", "api_key": "fixture"}} if provider != "custom" else {}
    monkeypatch.setattr(rp, "resolve_runtime_provider", REAL_RESOLVE_RUNTIME)
    monkeypatch.setattr("hermes_cli.config.get_compatible_custom_providers", REAL_CUSTOM_PROVIDERS)
    monkeypatch.setattr(rp, "_try_resolve_from_custom_pool", lambda *a, **kw: None)
    startup = REAL_RESOLVE_RUNTIME()
    assert (startup["provider"], startup["base_url"]) == ("custom", "https://models-dev.harso.dev/v1")
    native.agent.provider, native.agent.base_url = startup["provider"], startup["base_url"]
    lock(native, model="default-model", effort=None, fast=False)
    assert submit(native) == "streaming"
    expected = admission._model_lock_route(startup["provider"], startup["base_url"])
    assert admission._model_lock_route(native.agent.provider, native.agent.base_url) == expected
    assert submit(native, "second") == "streaming"
    lock(native, effort=None, fast=False)
    # A poisoned endpoint (even under a named-custom label) must be repaired.
    native.agent.base_url = "https://competitor.example/v1"
    assert submit(native, "healed") == "streaming"
    assert admission._model_lock_route(native.agent.provider, native.agent.base_url) == expected
    assert f"--provider {provider}" in [
        p["value"] for m, p in native.calls if m == "config.set"][-1]
