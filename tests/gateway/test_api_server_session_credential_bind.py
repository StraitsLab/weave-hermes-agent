"""Behavior contracts for native REST session credential binding."""

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import gateway.run as gateway_run
import hermes_state
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource
from gateway.platforms.api_server import APIServerAdapter
from gateway.run import GatewayRunner, _resolve_runtime_agent_kwargs
from hermes_state import SessionDB


SESSION_ID = "native-bind-session"
BEARER = "test-bearer-never-log"


def _expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat().replace(
        "+00:00", "Z"
    )


def _bind_body(**overrides):
    body = {
        "credential_slot": "GATE_B_API_KEY",
        "bearer": BEARER,
        "expires_at": _expiry(),
        "provider_route_revision_id": "opaque-route-revision",
    }
    body.update(overrides)
    return body


def _runtime_holder(runner, session_id):
    source = SessionSource(
        platform=Platform.API_SERVER, chat_id=session_id, chat_type="dm",
        user_id="api_server", user_name="API server",
    )
    session_key = runner.session_store._generate_session_key(source)
    _, runtime = runner._resolve_session_agent_runtime(
        source=source, session_key=session_key, user_config={"model": {"default": "test"}},
    )
    return runtime.get("api_key")


@pytest.fixture
def adapter_and_runner(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "ambient-key", "provider": "stock"},
    )
    runner = GatewayRunner(GatewayConfig())
    runner._running = True
    runner._session_db._db.create_session(SESSION_ID, "api_server")
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "sk-native-bind-test"})
    )
    adapter._session_db = runner._session_db._db
    adapter.gateway_runner = runner
    try:
        yield adapter, runner
    finally:
        runner._session_db._db.close()


@pytest.fixture
def gate_b_agent(adapter_and_runner, tmp_path, monkeypatch):
    """Real provider selection and bind/run plumbing; stop at the LLM boundary."""
    import yaml
    import run_agent

    adapter, runner = adapter_and_runner
    config = {
        "model": {"provider": "custom:weave-gate-b", "default": "pinned-model"},
        "custom_providers": [{
            "name": "weave-gate-b", "base_url": "https://gate-b.invalid/v1",
            "key_env": "GATE_B_API_KEY",
        }],
        "fallback_model": {"provider": "openrouter", "model": "fallback-model"},
        "platform_toolsets": {"api_server": []},
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    assert gateway_run._load_gateway_config()["model"]["provider"] == "custom:weave-gate-b"
    # Undo the general fixture's stock runtime stub for this integration path.
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", _resolve_runtime_agent_kwargs,
    )
    monkeypatch.setenv("GATE_B_API_KEY", "ambient-gate-b-key")
    monkeypatch.setattr(GatewayRunner, "_load_fallback_model", staticmethod(
        lambda: [{"provider": "openrouter", "model": "fallback-model"}],
    ))
    constructor = MagicMock()
    agent = constructor.return_value
    agent.run_conversation.return_value = {"final_response": "done"}
    agent.session_prompt_tokens = agent.session_completion_tokens = agent.session_total_tokens = 0
    monkeypatch.setattr(run_agent, "AIAgent", constructor)
    return adapter, runner, constructor


async def _finished_run(client, adapter, *, prefix="", **body):
    headers = {"Authorization": f"Bearer {adapter.config.extra['key']}"}
    response = await client.post(
        f"{prefix}/v1/runs", headers=headers,
        json={"session_id": SESSION_ID, "input": "test only", **body},
    )
    admitted = await response.json()
    assert response.status == 202, admitted
    task = adapter._active_run_tasks.get(admitted["run_id"])
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), 5)
    response = await client.get(f"{prefix}/v1/runs/{admitted['run_id']}", headers=headers)
    assert response.status == 200
    return await response.json()


@pytest.mark.asyncio
async def test_gate_b_run_uses_bound_callable_not_ambient_key(gate_b_agent):
    adapter, runner, constructor = gate_b_agent
    client = await _client(adapter)
    headers = {"Authorization": f"Bearer {adapter.config.extra['key']}"}
    try:
        bound = await client.post(
            f"/api/sessions/{SESSION_ID}/credential/bind", headers=headers, json=_bind_body(),
        )
        assert bound.status == 200
        result = await _finished_run(client, adapter, model="requested-pinned-model")
    finally:
        await client.close()
    assert result["status"] == "completed", result
    kwargs = constructor.call_args.kwargs
    assert callable(kwargs["api_key"])
    assert kwargs["api_key"]() == BEARER
    assert kwargs["model"] == "requested-pinned-model"
    assert kwargs["base_url"] == "https://gate-b.invalid/v1"
    assert kwargs["fallback_model"] is None
    assert kwargs.get("credential_pool") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["missing", "expired", "revoked", "wrong-session", "displaced", "no-runner"])
async def test_gate_b_run_refuses_unusable_binding(gate_b_agent, state):
    adapter, runner, constructor = gate_b_agent
    source = SessionSource(
        platform=Platform.API_SERVER, chat_id=SESSION_ID, chat_type="dm",
        user_id="api_server", user_name="API server",
    )
    if state != "missing":
        expires = datetime.now(timezone.utc) + timedelta(minutes=5)
        if state == "expired":
            expires -= timedelta(minutes=10)
        assert runner.bind_session_credential(source, SESSION_ID, BEARER, expires, "revision")
        if state == "revoked":
            runner.revoke_session_credential(source, SESSION_ID)
        if state == "displaced":
            runner.session_store.reset_session(runner.session_store._generate_session_key(source))
    if state == "no-runner":
        adapter.gateway_runner = None
    client = await _client(adapter)
    try:
        result = await _finished_run(
            client, adapter,
            session_id="unbound-session" if state == "wrong-session" else SESSION_ID,
        )
    finally:
        await client.close()
    assert result["status"] == "failed", result
    assert "credential unavailable" in result["error"]
    assert BEARER not in repr(result)
    constructor.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("update", ["refresh", "revoke", "expire"])
async def test_gate_b_run_holder_refresh_and_revoke_are_live(gate_b_agent, update):
    adapter, runner, constructor = gate_b_agent
    entered, proceed = threading.Event(), threading.Event()
    observed = []

    def conversation(**_kwargs):
        holder = constructor.call_args.kwargs["api_key"]
        observed.append(holder())
        entered.set()
        assert proceed.wait(5)
        observed.append(holder())
        return {"final_response": "done"}

    constructor.return_value.run_conversation.side_effect = conversation
    client = await _client(adapter)
    headers = {"Authorization": f"Bearer {adapter.config.extra['key']}"}
    task = None
    try:
        assert (await client.post(
            f"/api/sessions/{SESSION_ID}/credential/bind", headers=headers, json=_bind_body(),
        )).status == 200
        task = asyncio.create_task(_finished_run(client, adapter))
        assert await asyncio.to_thread(entered.wait, 5)
        holder = constructor.call_args.kwargs["api_key"]
        if update == "refresh":
            assert (await client.post(
                f"/api/sessions/{SESSION_ID}/credential/bind", headers=headers,
                json=_bind_body(bearer="refreshed-bearer"),
            )).status == 200
        elif update == "revoke":
            assert (await client.patch(
                f"/api/sessions/{SESSION_ID}", headers=headers, json={"end_reason": "closed"},
            )).status == 200
        else:
            assert holder.refresh(BEARER, datetime.now(timezone.utc) - timedelta(seconds=1))
        proceed.set()
        result = await task
        if update == "refresh":
            assert result["status"] == "completed", result
            assert observed == [BEARER, "refreshed-bearer"]
        else:
            assert result["status"] == "failed", result
            assert "credential unavailable" in result["error"]
            assert observed == [BEARER]
            with pytest.raises(RuntimeError, match="credential unavailable"):
                holder()
    finally:
        proceed.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
async def test_gate_b_run_separates_two_sessions(gate_b_agent):
    adapter, runner, constructor = gate_b_agent
    runner._session_db._db.create_session("second-session", "api_server")
    client = await _client(adapter)
    headers = {"Authorization": f"Bearer {adapter.config.extra['key']}"}
    holders = []
    try:
        for session_id, bearer in ((SESSION_ID, BEARER), ("second-session", "second-bearer")):
            assert (await client.post(
                f"/api/sessions/{session_id}/credential/bind", headers=headers,
                json=_bind_body(bearer=bearer),
            )).status == 200
            result = await _finished_run(client, adapter, session_id=session_id)
            assert result["status"] == "completed", result
            holders.append(constructor.call_args.kwargs["api_key"])
        assert holders[0] is not holders[1]
        assert holders[0]() == BEARER
        assert holders[1]() == "second-bearer"
        await client.patch(f"/api/sessions/{SESSION_ID}", headers=headers, json={"end_reason": "closed"})
        with pytest.raises(RuntimeError, match="credential unavailable"):
            holders[0]()
        assert holders[1]() == "second-bearer"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_gate_b_runs_use_exact_profile_holder(gate_b_agent, tmp_path, monkeypatch):
    adapter, runner, constructor = gate_b_agent
    runner.config.multiplex_profiles = True
    adapter._session_db = None
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH)
    config_text = (tmp_path / "config.yaml").read_text()
    for name in ("alpha", "beta"):
        home = tmp_path / "profiles" / name
        home.mkdir(parents=True)
        (home / "config.yaml").write_text(config_text)
        (home / ".env").write_text(f"API_SERVER_KEY={adapter.config.extra['key']}\n")
        db = SessionDB(home / "state.db")
        db.create_session(SESSION_ID, "api_server")
        db.close()
    client = await _profile_client(adapter)
    headers = {"Authorization": f"Bearer {adapter.config.extra['key']}"}
    try:
        assert (await client.post(
            f"/p/alpha/api/sessions/{SESSION_ID}/credential/bind", headers=headers,
            json=_bind_body(bearer="alpha-bearer"),
        )).status == 200
        result = await _finished_run(client, adapter, prefix="/p/beta")
        assert result["status"] == "failed", result
        constructor.assert_not_called()
        for name in ("alpha", "beta"):
            assert (await client.post(
                f"/p/{name}/api/sessions/{SESSION_ID}/credential/bind", headers=headers,
                json=_bind_body(bearer=f"{name}-bearer"),
            )).status == 200
            result = await _finished_run(client, adapter, prefix=f"/p/{name}")
            assert result["status"] == "completed", result
            assert constructor.call_args.kwargs["api_key"]() == f"{name}-bearer"
    finally:
        await client.close()
        runner.session_store.close_all_db_handles()
        for db in adapter._session_dbs.values():
            db.close()


@pytest.mark.parametrize("selection", [
    {"requested_provider": "openrouter"},
    {"route": {"model": "route-model", "base_url": "https://other.invalid/v1"}},
])
def test_gate_b_create_agent_refuses_route_escape(gate_b_agent, selection):
    from gateway.platforms.api_server import _ProviderAuthResolutionError

    adapter, runner, constructor = gate_b_agent
    source = SessionSource(platform=Platform.API_SERVER, chat_id=SESSION_ID, chat_type="dm")
    assert runner.bind_session_credential(
        source, SESSION_ID, BEARER, datetime.now(timezone.utc) + timedelta(minutes=5), "revision",
    )
    with pytest.raises(_ProviderAuthResolutionError, match="route mismatch"):
        adapter._create_agent(session_id=SESSION_ID, **selection)
    constructor.assert_not_called()


def test_gate_b_create_agent_preserves_route_and_has_no_ambient_dependency(gate_b_agent, monkeypatch):
    adapter, runner, constructor = gate_b_agent
    source = SessionSource(platform=Platform.API_SERVER, chat_id=SESSION_ID, chat_type="dm")
    assert runner.bind_session_credential(
        source, SESSION_ID, BEARER, datetime.now(timezone.utc) + timedelta(minutes=5), "revision",
    )
    monkeypatch.delenv("GATE_B_API_KEY", raising=False)
    ambient = MagicMock(side_effect=RuntimeError("ambient resolution must not run"))
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", ambient)
    adapter._create_agent(session_id=SESSION_ID, route={
        "provider": "custom:weave-gate-b", "model": "route-pinned-model",
        "base_url": "https://gate-b.invalid/v1", "api_key": "route-key-must-not-win",
    })
    kwargs = constructor.call_args.kwargs
    assert kwargs["api_key"]() == BEARER
    assert kwargs["model"] == "route-pinned-model"
    assert kwargs["base_url"] == "https://gate-b.invalid/v1"
    assert kwargs["fallback_model"] is None
    ambient.assert_not_called()


def test_non_gate_b_create_agent_keeps_upstream_credentials_and_fallback(gate_b_agent, monkeypatch):
    adapter, runner, constructor = gate_b_agent
    source = SessionSource(platform=Platform.API_SERVER, chat_id=SESSION_ID, chat_type="dm")
    assert runner.bind_session_credential(
        source, SESSION_ID, BEARER, datetime.now(timezone.utc) + timedelta(minutes=5), "revision",
    )
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {"model": {"provider": "stock"}})
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {
        "api_key": "upstream-key", "provider": "stock", "base_url": "https://stock.invalid/v1",
    })
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda: "upstream-model")
    adapter._create_agent(session_id=SESSION_ID)
    kwargs = constructor.call_args.kwargs
    assert kwargs["api_key"] == "upstream-key"
    assert kwargs["provider"] == "stock"
    assert kwargs["model"] == "upstream-model"
    assert kwargs["fallback_model"] == [{"provider": "openrouter", "model": "fallback-model"}]


def test_gate_b_create_agent_does_not_recover_an_unpinned_model(gate_b_agent, monkeypatch):
    from gateway.platforms.api_server import _ProviderAuthResolutionError

    adapter, runner, constructor = gate_b_agent
    source = SessionSource(platform=Platform.API_SERVER, chat_id=SESSION_ID, chat_type="dm")
    assert runner.bind_session_credential(
        source, SESSION_ID, BEARER, datetime.now(timezone.utc) + timedelta(minutes=5), "revision",
    )
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda: "")
    adapter._last_resolved_model["*"] = "another-sessions-model"
    with pytest.raises(_ProviderAuthResolutionError, match="model unavailable"):
        adapter._create_agent(session_id=SESSION_ID)
    constructor.assert_not_called()


async def _client(adapter):
    app = web.Application()
    for method, path, handler in adapter._http_route_table():
        app.router.add_route(method, path, handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def _profile_client(adapter):
    app = web.Application(middlewares=[adapter._make_profile_prefix_middleware()])
    for method, path, handler in adapter._http_route_table():
        app.router.add_route(method, path, handler)
        app.router.add_route(method, "/p/{profile}" + path, handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_authenticated_exact_shape_bind_is_ready_and_secret_free(adapter_and_runner):
    adapter, runner = adapter_and_runner
    client = await _client(adapter)
    try:
        response = await client.post(
            f"/api/sessions/{SESSION_ID}/credential/bind",
            headers={"Authorization": "Bearer sk-native-bind-test"},
            json=_bind_body(),
        )
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 200
    assert body == {"status": "ready", "credential_slot": "GATE_B_API_KEY"}
    assert BEARER not in repr(body)
    holder = _runtime_holder(runner, SESSION_ID)
    assert holder is not None
    assert holder() == BEARER


@pytest.mark.asyncio
async def test_bind_distinguishes_unknown_from_malformed_and_ended_sessions(adapter_and_runner):
    adapter, runner = adapter_and_runner
    client = await _client(adapter)
    headers = {"Authorization": "Bearer sk-native-bind-test"}
    await asyncio.to_thread(
        runner._session_db._db.create_session, "ended-native-bind-session", "api_server"
    )
    await asyncio.to_thread(
        runner._session_db._db.end_session, "ended-native-bind-session", "closed"
    )
    try:
        malformed = await client.post(
            f"/api/sessions/{SESSION_ID}/credential/bind", headers=headers,
            json={"bearer": BEARER},
        )
        unknown = await client.post(
            "/api/sessions/missing-native-bind-session/credential/bind",
            headers=headers, json=_bind_body(),
        )
        ended = await client.post(
            "/api/sessions/ended-native-bind-session/credential/bind",
            headers=headers, json=_bind_body(),
        )
        malformed_body, unknown_body, ended_body = (
            await malformed.json(), await unknown.json(), await ended.json(),
        )
    finally:
        await client.close()

    assert malformed.status == 400
    assert malformed_body["error"]["code"] == "invalid_session_credential_schema"
    assert unknown.status == 404
    assert unknown_body["error"]["code"] == "session_not_found"
    assert ended.status == 409
    assert ended_body["error"]["code"] == "session_reset_required"
    assert "Retry-After" not in ended.headers
    assert runner._session_db._db.get_session("ended-native-bind-session")["end_reason"] == "closed"


@pytest.mark.asyncio
@pytest.mark.parametrize("end_original", [False, True], ids=["displaced-live", "reset-ended"])
async def test_persisted_reset_route_requires_reset_without_binding_a_credential(
    adapter_and_runner, end_original, caplog,
):
    adapter, runner = adapter_and_runner
    store = runner.session_store
    source = SessionSource(
        platform=Platform.API_SERVER, chat_id=SESSION_ID, chat_type="dm",
        user_id="api_server", user_name="API server",
    )
    entry = store.bind_existing_session(source, SESSION_ID, reopen=False)
    runner._session_db._db.append_message(SESSION_ID, "user", "keep this history")
    descendant = store.reset_session(entry.session_key)
    db = runner._session_db._db
    if not end_original:
        # A failed end write can leave the displaced parent live.
        db.reopen_session(SESSION_ID)
    with store._lock:
        store._entries.clear()
        store._loaded = False
    store._ensure_loaded()
    assert store.lookup_by_session_key(entry.session_key).session_id == descendant.session_id
    before_messages = db.get_messages(SESSION_ID)
    client = await _client(adapter)
    try:
        response = await client.post(
            f"/api/sessions/{SESSION_ID}/credential/bind",
            headers={"Authorization": f"Bearer {adapter.config.extra['key']}"},
            json=_bind_body(),
        )
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 409
    assert body["error"]["code"] == "session_reset_required"
    assert "Retry-After" not in response.headers
    cause = "session_ended" if end_original else "source_displaced"
    assert f"native_session_reset_required cause={cause}" in caplog.text
    assert descendant.session_id not in repr(body) + caplog.text
    assert BEARER not in repr(body) + caplog.text
    assert store.lookup_by_session_key(entry.session_key).session_id == descendant.session_id
    state = runner._peek_session_state(entry.session_key)
    assert state is None or state.conversation.credential_holder is None
    assert (db.get_session(SESSION_ID)["end_reason"] is not None) == end_original
    assert db.get_messages(SESSION_ID) == before_messages


@pytest.mark.asyncio
async def test_bind_waiting_on_routing_lock_observes_a_closed_session(
    adapter_and_runner, monkeypatch,
):
    """Closing a row before the routing lock is acquired must defeat strict bind."""
    import threading

    adapter, runner = adapter_and_runner
    store = runner.session_store
    entered = threading.Event()
    routing_lock = store._lock
    generate_key = store._generate_session_key

    class ObservedLock:
        def acquire(self, *args, **kwargs):
            return routing_lock.acquire(*args, **kwargs)

        def release(self):
            routing_lock.release()

        def __enter__(self):
            entered.set()
            self.acquire()
            return self

        def __exit__(self, *_args):
            self.release()

    monkeypatch.setattr(store, "_lock", ObservedLock())
    client = await _client(adapter)
    store._lock.acquire()
    try:
        binding = asyncio.create_task(client.post(
            f"/api/sessions/{SESSION_ID}/credential/bind",
            headers={"Authorization": f"Bearer {adapter.config.extra['key']}"},
            json=_bind_body(),
        ))
        assert await asyncio.to_thread(entered.wait, 5)
        await asyncio.to_thread(runner._session_db._db.end_session, SESSION_ID, "closed")
    finally:
        store._lock.release()
    try:
        response = await asyncio.wait_for(binding, 5)
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 409
    assert body["error"]["code"] == "session_reset_required"
    assert "Retry-After" not in response.headers
    key = generate_key(SessionSource(platform=Platform.API_SERVER, chat_id=SESSION_ID, chat_type="dm"))
    state = runner._peek_session_state(key)
    assert state is None or state.conversation.credential_holder is None
    assert runner._session_db._db.get_session(SESSION_ID)["end_reason"] == "closed"


@pytest.mark.asyncio
async def test_reset_after_strict_bind_does_not_publish_a_holder(
    adapter_and_runner, monkeypatch,
):
    adapter, runner = adapter_and_runner
    store = runner.session_store
    bind = store.bind_existing_session
    reset = threading.Event()
    allow_return = threading.Event()

    def paused_bind(*args, **kwargs):
        entry = bind(*args, **kwargs)
        reset.set()
        assert allow_return.wait(5)
        return entry

    monkeypatch.setattr(store, "bind_existing_session", paused_bind)
    client = await _client(adapter)
    binding = asyncio.create_task(client.post(
        f"/api/sessions/{SESSION_ID}/credential/bind",
        headers={"Authorization": f"Bearer {adapter.config.extra['key']}"}, json=_bind_body(),
    ))
    try:
        assert await asyncio.to_thread(reset.wait, 5)
        source = SessionSource(platform=Platform.API_SERVER, chat_id=SESSION_ID, chat_type="dm")
        key = store._generate_session_key(source)
        descendant = await asyncio.to_thread(store.reset_session, key)
        allow_return.set()
        response = await asyncio.wait_for(binding, 5)
        body = await response.json()
    finally:
        allow_return.set()
        await client.close()

    assert response.status == 409
    assert body["error"]["code"] == "session_reset_required"
    assert store.lookup_by_session_key(key).session_id == descendant.session_id
    state = runner._peek_session_state(key)
    assert state is None or state.conversation.credential_holder is None


@pytest.mark.asyncio
async def test_bind_refreshes_one_same_revision_holder_and_boundary_revokes_it(
    adapter_and_runner,
):
    adapter, runner = adapter_and_runner
    client = await _client(adapter)
    headers = {"Authorization": "Bearer sk-native-bind-test"}
    try:
        first = await client.post(
            f"/api/sessions/{SESSION_ID}/credential/bind", headers=headers,
            json=_bind_body(),
        )
        holder = _runtime_holder(runner, SESSION_ID)
        refresh = await client.post(
            f"/api/sessions/{SESSION_ID}/credential/bind", headers=headers,
            json=_bind_body(bearer="replacement-bearer"),
        )
        changed = await client.post(
            f"/api/sessions/{SESSION_ID}/credential/bind", headers=headers,
            json=_bind_body(provider_route_revision_id="different-revision"),
        )
        expired = await client.post(
            f"/api/sessions/{SESSION_ID}/credential/bind", headers=headers,
            json=_bind_body(expires_at="2000-01-01T00:00:00Z"),
        )
        malformed_time = await client.post(f"/api/sessions/{SESSION_ID}/credential/bind", headers=headers, json=_bind_body(expires_at="2026-99-01T00:00:00Z"))
        changed_body, expired_body, malformed_time_body = await changed.json(), await expired.json(), await malformed_time.json()
    finally:
        await client.close()

    assert first.status == refresh.status == 200
    assert _runtime_holder(runner, SESSION_ID) is holder
    assert holder() == "replacement-bearer"
    assert changed.status == expired.status == 409
    assert changed_body["error"]["code"] == expired_body["error"]["code"] == "credential_unavailable"
    assert malformed_time.status == 400
    assert malformed_time_body["error"]["code"] == "invalid_session_credential_schema"
    runner._clear_conversation_scope(
        "agent:main:api_server:dm:native-bind-session", reason="test"
    )
    with pytest.raises(RuntimeError, match="credential unavailable"):
        holder()


@pytest.mark.asyncio
async def test_rest_end_and_delete_revoke_live_session_credentials(adapter_and_runner):
    adapter, runner = adapter_and_runner
    client = await _client(adapter)
    headers = {"Authorization": "Bearer sk-native-bind-test"}
    try:
        await client.post(
            f"/api/sessions/{SESSION_ID}/credential/bind", headers=headers, json=_bind_body(),
        )
        ended_holder = _runtime_holder(runner, SESSION_ID)
        ended = await client.patch(
            f"/api/sessions/{SESSION_ID}", headers=headers, json={"end_reason": "closed"},
        )
        runner._session_db._db.create_session("native-delete-session", "api_server")
        await client.post(
            "/api/sessions/native-delete-session/credential/bind", headers=headers,
            json=_bind_body(),
        )
        deleted_holder = _runtime_holder(runner, "native-delete-session")
        deleted = await client.delete(
            "/api/sessions/native-delete-session", headers=headers,
        )
    finally:
        await client.close()

    assert ended.status == deleted.status == 200
    with pytest.raises(RuntimeError, match="credential unavailable"):
        ended_holder()
    with pytest.raises(RuntimeError, match="credential unavailable"):
        deleted_holder()


@pytest.mark.asyncio
async def test_named_profile_routes_isolate_same_session_id_and_credentials(tmp_path, monkeypatch):
    """Two profile-routed sessions may share an ID but never a holder or route."""
    root = tmp_path / ".hermes"
    alpha, beta = root / "profiles" / "alpha", root / "profiles" / "beta"
    alpha.mkdir(parents=True)
    beta.mkdir(parents=True)
    (alpha / ".env").write_text("API_SERVER_KEY=sk-alpha-profile-test\n")
    (beta / ".env").write_text("API_SERVER_KEY=sk-beta-profile-test\n")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "ambient"})
    runner = GatewayRunner(GatewayConfig(multiplex_profiles=True))
    runner._running = True
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "sk-profile-bind-test"}))
    adapter.gateway_runner = runner
    alpha_db, beta_db = SessionDB(alpha / "state.db"), SessionDB(beta / "state.db")
    alpha_db.create_session("shared-session", "api_server")
    beta_db.create_session("shared-session", "api_server")
    client = await _profile_client(adapter)
    try:
        alpha_bind = await client.post("/p/alpha/api/sessions/shared-session/credential/bind", headers={"Authorization": "Bearer sk-alpha-profile-test"}, json=_bind_body(bearer="alpha-bearer"))
        beta_bind = await client.post("/p/beta/api/sessions/shared-session/credential/bind", headers={"Authorization": "Bearer sk-beta-profile-test"}, json=_bind_body(bearer="beta-bearer"))
        cross = await client.post("/p/alpha/api/sessions/shared-session/credential/bind", headers={"Authorization": "Bearer sk-alpha-profile-test"}, json=_bind_body(bearer="beta-bearer", provider_route_revision_id="beta-revision"))
        beta_refresh = await client.post("/p/beta/api/sessions/shared-session/credential/bind", headers={"Authorization": "Bearer sk-beta-profile-test"}, json=_bind_body(bearer="beta-refresh"))
        alpha_body, beta_body = await alpha_bind.json(), await beta_bind.json()
    finally:
        await client.close()
        alpha_db.close()
        beta_db.close()
        runner.session_store.close_all_db_handles()

    assert alpha_bind.status == beta_bind.status == 200, (
        alpha_body, beta_body, list(runner.session_store._db_handles),
    )
    assert cross.status == 409
    assert beta_refresh.status == 200
    assert alpha_body == beta_body == {"status": "ready", "credential_slot": "GATE_B_API_KEY"}
    alpha_source = SessionSource(platform=Platform.API_SERVER, chat_id="shared-session", chat_type="dm", user_id="api_server", profile="alpha")
    beta_source = SessionSource(platform=Platform.API_SERVER, chat_id="shared-session", chat_type="dm", user_id="api_server", profile="beta")
    assert runner.session_credential_available(alpha_source, "shared-session")
    assert runner.session_credential_available(beta_source, "shared-session")
    assert runner._peek_session_state(runner.session_store._generate_session_key(alpha_source)).conversation.credential_holder() == "alpha-bearer"
    assert runner._peek_session_state(runner.session_store._generate_session_key(beta_source)).conversation.credential_holder() == "beta-refresh"


@pytest.mark.asyncio
async def test_close_wins_queued_submit_without_reopen_or_durable_admission(adapter_and_runner):
    """The real lifecycle lock makes close win before native submit admission."""
    adapter, runner = adapter_and_runner
    client = await _client(adapter)
    headers = {"Authorization": "Bearer sk-native-bind-test"}
    try:
        for suffix, delete in (("end", False), ("delete", True)):
            session_id = f"barrier-{suffix}"
            runner._session_db._db.create_session(session_id, "api_server")
            assert (await client.post(f"/api/sessions/{session_id}/credential/bind", headers=headers, json=_bind_body())).status == 200
            async with adapter._native_lifecycle_lock(session_id):
                closed_task = asyncio.create_task(client.delete(f"/api/sessions/{session_id}", headers=headers) if delete else client.patch(f"/api/sessions/{session_id}", headers=headers, json={"end_reason": "closed"}))
                key = ("default", session_id)
                for _ in range(100):
                    if adapter._native_submit_lock_refs.get(key, 0) == 2:
                        break
                    await asyncio.sleep(0.001)
                assert adapter._native_submit_lock_refs.get(key, 0) == 2
                submit_task = asyncio.create_task(client.post(f"/api/sessions/{session_id}/submit", headers=headers, json={"kind": "hermes.session.submit", "external_request_id": f"barrier-{suffix}", "message": "hello", "busy_mode": "queue"}))
                for _ in range(100):
                    if adapter._native_submit_lock_refs.get(key, 0) == 3:
                        break
                    await asyncio.sleep(0.001)
                assert adapter._native_submit_lock_refs.get(key, 0) == 3
            closed, submit = await asyncio.gather(closed_task, submit_task)
            assert closed.status == 200
            assert submit.status == 409
            row = runner._session_db._db.get_session(session_id)
            assert (row is None) is delete
            assert delete or row["end_reason"] == "closed"
            assert not adapter._native_submit_ref_sessions
            conn = runner._session_db._db._conn
            assert not conn.execute("SELECT name FROM sqlite_master WHERE name='native_session_submit_idempotency'").fetchone()
    finally:
        await client.close()
