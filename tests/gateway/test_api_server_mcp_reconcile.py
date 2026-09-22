"""Authenticated root reconcile HTTP contract (loopback only)."""
import asyncio
import threading

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from tools import mcp_tool as mcp
from tests.tools.test_mcp_reconcile import runtime, _start, _names

KEY = "reconcile-test-key-not-a-secret"
HEADERS = {"Authorization": f"Bearer {KEY}"}


@pytest_asyncio.fixture
async def client():
    adapter = APIServerAdapter(PlatformConfig(extra={"key": KEY}))
    app = web.Application()
    for method, path, handler in adapter._http_route_table():
        app.router.add_route(method, path, handler)
    async with TestClient(TestServer(app, host="127.0.0.1")) as client:
        yield client


@pytest.mark.asyncio
async def test_auth_required(client, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("unauthenticated request reached reconcile")
    monkeypatch.setattr(mcp, "reconcile_mcp_servers", forbidden)
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        response = await client.post("/v1/mcp/reconcile", json={"servers": {}}, headers=headers)
        assert response.status == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [None, [], {}, {"servers": []}, {"servers": None}, {"servers": {"a": 2}}, {"servers": {"a": []}}, {"servers": {"": {}}}])
async def test_invalid_body(client, monkeypatch, body):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid body reached reconcile")
    monkeypatch.setattr(mcp, "reconcile_mcp_servers", forbidden)
    response = await client.post("/v1/mcp/reconcile", json=body, headers=HEADERS)
    assert response.status == 400


@pytest.mark.asyncio
async def test_invalid_json(client):
    response = await client.post("/v1/mcp/reconcile", data="{", headers=HEADERS)
    assert response.status == 400


@pytest.mark.asyncio
async def test_outcomes_and_off_loop_execution(client, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    loop_thread = threading.get_ident()
    captured = []
    def slow_reconcile(servers):
        captured.append((servers, threading.get_ident()))
        entered.set()
        assert release.wait(3), "event loop did not release worker"
        return {"fixture": "refused_busy"}
    monkeypatch.setattr(mcp, "reconcile_mcp_servers", slow_reconcile)
    servers = {"fixture": {"command": "fixture"}}
    request = asyncio.create_task(client.post("/v1/mcp/reconcile", json={"servers": servers}, headers=HEADERS))
    try:
        for _ in range(300):
            if entered.is_set():
                break
            await asyncio.sleep(.01)
        assert entered.is_set()
        assert not request.done()
        # This coroutine can make progress while the reconcile worker waits.
        response = await client.get("/health")
        assert response.status == 200
    finally:
        release.set()
    response = await request
    assert response.status == 200
    assert await response.json() == {"outcomes": {"fixture": "refused_busy"}}
    assert captured[0][0] == servers
    assert captured[0][1] != loop_thread


@pytest.mark.asyncio
async def test_internal_error_is_not_disclosed(client, monkeypatch):
    def fail(servers):
        raise RuntimeError("private descriptor secret")
    monkeypatch.setattr(mcp, "reconcile_mcp_servers", fail)
    response = await client.post("/v1/mcp/reconcile", json={"servers": {}}, headers=HEADERS)
    assert response.status == 500
    assert "private descriptor secret" not in await response.text()


@pytest.mark.asyncio
async def test_http_reconcile_replaces_real_stdio_connection(client, runtime):
    old = await asyncio.to_thread(_start, runtime)
    cfg = runtime.descriptor("HTTP-CONTROL", tools={"include": ["beta"]})
    response = await client.post("/v1/mcp/reconcile", json={"servers": {"fixture": cfg}}, headers=HEADERS)
    assert response.status == 200
    assert await response.json() == {"outcomes": {"fixture": "replaced"}}
    new = mcp._servers["fixture"]
    assert new is not old
    assert old.session is None
    assert new._config == cfg
    assert set(new._registered_tool_names) == _names(tools=("beta",))
    result = await asyncio.to_thread(mcp._make_tool_handler("fixture", "beta", 5), {})
    assert "HTTP-CONTROL:beta" in result


@pytest.mark.asyncio
async def test_named_profile_cannot_claim_root_authority(monkeypatch):
    from aiohttp.test_utils import make_mocked_request
    from gateway.platforms.api_server import _api_request_profile
    adapter = APIServerAdapter(PlatformConfig(extra={"key": KEY}))
    monkeypatch.setattr(adapter, "_expected_api_key", lambda: KEY)
    token = _api_request_profile.set("sibling")
    try:
        request = make_mocked_request("POST", "/v1/mcp/reconcile", headers=HEADERS)
        response = await adapter._handle_mcp_reconcile(request)
        assert response.status == 403
    finally:
        _api_request_profile.reset(token)
