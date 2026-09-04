"""Behavior contracts for native REST session credential binding."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import gateway.run as gateway_run
import hermes_state
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource
from gateway.platforms.api_server import APIServerAdapter
from gateway.run import GatewayRunner
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
    assert ended_body["error"]["code"] == "credential_unavailable"
    assert runner._session_db._db.get_session("ended-native-bind-session")["end_reason"] == "closed"


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
