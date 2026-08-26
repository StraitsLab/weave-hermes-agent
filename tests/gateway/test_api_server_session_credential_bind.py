"""Behavior contracts for native REST session credential binding."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource
from gateway.platforms.api_server import APIServerAdapter
from gateway.run import GatewayRunner


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
        changed_body, expired_body = await changed.json(), await expired.json()
    finally:
        await client.close()

    assert first.status == refresh.status == 200
    assert _runtime_holder(runner, SESSION_ID) is holder
    assert holder() == "replacement-bearer"
    assert changed.status == expired.status == 409
    assert changed_body["error"]["code"] == expired_body["error"]["code"] == "credential_unavailable"
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
