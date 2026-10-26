"""The native MessageEvent path must honor a persisted chat pick, without changing the profile."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import GatewayConfig, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.run import GatewayRunner
from gateway.session import SessionStore
from hermes_state import AsyncSessionDB, SessionDB


@pytest.fixture
def native(tmp_path, monkeypatch):
    import hermes_yaml as yaml
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    config = {"gateway": {"native_bot_sessions": True},
              "model": {"default": "profile-model", "provider": "custom:weave-gate-b",
                        "base_url": "https://models-dev.harso.dev/v1"},
              "custom_providers": [{"name": "weave-gate-b", "base_url": "https://models-dev.harso.dev/v1",
                                    "api_key": "test-only", "models": ["profile-model", "chat-model", "next-model"]}]}
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    monkeypatch.setattr("gateway.run._hermes_home", home)
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda **kw: config)
    route = {"provider": "custom", "base_url": config["model"]["base_url"], "api_key": "test-only",
             "api_mode": "chat_completions"}
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs", lambda: dict(route))
    db = SessionDB(home / "state.db")
    db.create_session("chat", "api_server", model="profile-model")
    store = SessionStore(home / "sessions", GatewayConfig())
    store._db = db
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.session_store = store
    runner._running = True
    runner._session_db = AsyncSessionDB(db)
    runner._evict_cached_agent = lambda key: None
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter.gateway_runner = runner
    adapter._ensure_session_db_async = AsyncMock(return_value=db)
    adapter._native_submit_answers_approval = lambda *args: None
    adapter._record_native_submit_handoff = lambda *args: None
    adapter._wait_native_submit_start = AsyncMock(return_value="streaming")
    seen = []

    async def dispatch(event):
        key = event.metadata["gateway_session_key"]
        model, runtime = runner._resolve_session_agent_runtime(source=event.source, session_key=key, user_config=config)
        seen.append((model, runtime["base_url"],
                     runner._resolve_session_reasoning_config(session_key=key, model=model),
                     runner._resolve_session_service_tier(session_key=key)))
    adapter.handle_message = AsyncMock(side_effect=dispatch)
    monkeypatch.setattr("hermes_cli.context_switch_guard.enrich_model_switch_warnings_for_gateway", lambda *a, **kw: None)
    yield SimpleNamespace(adapter=adapter, runner=runner, db=db, seen=seen, home=home, config=config)
    db.close()


def pick(n, model="chat-model", effort="high", fast=True):
    n.db.update_session_runtime_lock("chat", model=model, provider="custom:weave-gate-b",
        model_options={"reasoning": {"effort": effort}, "fast": fast}, confirmed=True)


async def submit(n, ref="first"):
    return await n.adapter._admit_native_session_submit("chat", "hello", ref)


@pytest.mark.asyncio
async def test_native_submit_uses_lock_and_reads_again_between_turns(native):
    n = native
    original = (n.home / "config.yaml").read_bytes()
    pick(n)
    assert await submit(n) == "streaming"
    assert n.adapter._session_response(n.db.get_session("chat"))["model_lock"] == {
        "model": "chat-model", "effort": "high", "fast": True}
    # Even if in-memory overrides are cleared (gateway restart), the next submit re-reads the intact lock.
    n.runner._session_model_overrides.clear()
    n.runner._session_reasoning_overrides.clear()
    n.runner._session_service_tier_overrides.clear()
    assert await submit(n, "after-restart") == "streaming"
    pick(n, "next-model", "low", False)
    assert await submit(n, "second") == "streaming"
    assert n.adapter._session_response(n.db.get_session("chat"))["model_lock"] == {
        "model": "next-model", "effort": "low", "fast": False}
    assert n.seen == [
        ("chat-model", "https://models-dev.harso.dev/v1", {"enabled": True, "effort": "high"}, "priority"),
        ("chat-model", "https://models-dev.harso.dev/v1", {"enabled": True, "effort": "high"}, "priority"),
        ("next-model", "https://models-dev.harso.dev/v1", {"enabled": True, "effort": "low"}, None)]
    assert (n.home / "config.yaml").read_bytes() == original


@pytest.mark.asyncio
async def test_no_lock_leaves_profile_and_other_session_settings_alone(native):
    n = native
    await submit(n)
    assert n.seen[0][0] == "profile-model"
    key = next(iter(n.runner.session_store._entries))
    n.runner._set_session_reasoning_override(key, {"enabled": False})
    n.runner._set_session_service_tier_override(key, "priority")
    await submit(n, "second")
    assert n.seen[-1][2:] == ({"enabled": False}, "priority")


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("target_provider", "openrouter"), ("base_url", "https://other.invalid/v1")])
async def test_model_resolution_cannot_reroute_the_chat(native, monkeypatch, field, value):
    from hermes_cli.model_switch import ModelSwitchResult
    pick(native)
    result = ModelSwitchResult(success=True, new_model="chat-model", target_provider="custom:weave-gate-b",
                               base_url="https://models-dev.harso.dev/v1", api_key="test-only")
    setattr(result, field, value)
    monkeypatch.setattr("hermes_cli.model_switch.switch_model", lambda **kw: result)
    with pytest.raises(RuntimeError, match="configured provider route"):
        await submit(native)
    native.adapter.handle_message.assert_not_called()
    assert not native.runner._session_model_override(next(iter(native.runner.session_store._entries)))


@pytest.mark.asyncio
async def test_get_session_projects_only_the_safe_lock(native):
    app = web.Application()
    app.router.add_get("/api/sessions/{session_id}", native.adapter._handle_get_session)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/api/sessions/chat")
        assert (await response.json())["session"]["model_lock"] is None
        pick(native)
        response = await client.get("/api/sessions/chat")
        row = (await response.json())["session"]
    assert row["model_lock"] == {"model": "chat-model", "effort": "high", "fast": True}
    assert "model_config" not in row
    assert "test-only" not in json.dumps(row)


@pytest.mark.asyncio
async def test_same_model_can_change_effort_and_fast_without_reswitching(native, monkeypatch):
    pick(native)
    await submit(native)
    monkeypatch.setattr("hermes_cli.model_switch.switch_model", lambda **kw: pytest.fail("unneeded model switch"))
    pick(native, effort="low", fast=False)
    await submit(native, "second")
    assert native.seen[-1][2:] == ({"enabled": True, "effort": "low"}, None)


@pytest.mark.asyncio
async def test_unconfirmed_lock_does_not_apply(native):
    native.db.update_session_runtime_lock("chat", model="chat-model", confirmed=False)
    await submit(native)
    assert native.seen[0][0] == "profile-model"
    assert native.adapter._session_response(native.db.get_session("chat"))["model_lock"] is None


def test_model_config_reader_does_not_flush_usage(native, monkeypatch):
    pick(native)
    monkeypatch.setattr(native.db, "flush_token_counts", lambda: pytest.fail("usage flush on admission"))
    assert native.db.get_session_model_config("chat")["browser_model_lock"]["model"] == "chat-model"


def test_safe_fork_keeps_the_confirmed_model_lock(native):
    pick(native)
    parent = json.loads(native.db.get_session("chat")["model_config"])
    assert native.db.safe_fork_session("chat", "child") == "forked"
    child = json.loads(native.db.get_session("child")["model_config"])
    assert child["browser_model_lock"] == parent["browser_model_lock"]
    assert child["_branched_from"] == "chat"


@pytest.mark.asyncio
async def test_inline_bare_custom_key_reaches_real_turn_resolution(native):
    n = native
    n.config['model'].update(provider='custom', api_key='review-inline-config-key')
    n.config['custom_providers'] = []
    import hermes_yaml as yaml
    (n.home / 'config.yaml').write_text(yaml.safe_dump(n.config))
    from hermes_cli.runtime_provider import resolve_runtime_provider
    baseline = resolve_runtime_provider()
    assert baseline['api_key'] == 'review-inline-config-key'
    assert baseline['base_url'] == n.config['model']['base_url']
    seen = []
    async def dispatch(event):
        model, runtime = n.runner._resolve_session_agent_runtime(source=event.source,
            session_key=event.metadata['gateway_session_key'], user_config=n.config)
        seen.append((model, runtime['api_key'], runtime['base_url']))
    n.adapter.handle_message.side_effect = dispatch
    n.db.update_session_runtime_lock('chat', model='chat-model', provider='custom', confirmed=True)
    await submit(n)
    assert seen == [('chat-model', 'review-inline-config-key', n.config['model']['base_url'])]
