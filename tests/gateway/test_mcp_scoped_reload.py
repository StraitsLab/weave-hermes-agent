import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch
import pytest
from tests.gateway.test_api_server_mcp_reload import _make_adapter, _make_request, _make_bare_runner, API_KEY
from gateway.platforms.api_server import _api_request_profile


def request(body=None, **kwargs):
    req = _make_request(**kwargs)
    req.can_read_body = True
    req.json = AsyncMock(return_value={"server": "weave-connectors"} if body is None else body)
    return req


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"server": "exa"}, {}, {"server": "weave-connectors", "extra": True}, [], {"server": None}])
async def test_unknown_selector_rejected(body):
    runner = SimpleNamespace(_reload_mcp_toolset=AsyncMock())
    response = await _make_adapter(runner)._handle_mcp_refresh(request(body))
    assert response.status == 400
    runner._reload_mcp_toolset.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_is_root_only_and_auth_precedes_parsing():
    runner = SimpleNamespace(_reload_mcp_toolset=AsyncMock())
    adapter = _make_adapter(runner)
    adapter._expected_api_key = lambda: API_KEY
    profile = _api_request_profile.set("worker")
    try:
        for token, status in [(None, 401), ("wrong-key", 401), (API_KEY, 403)]:
            req = request(token=token)
            assert (await adapter._handle_mcp_refresh(req)).status == status
            req.json.assert_not_called()
    finally:
        _api_request_profile.reset(profile)
    runner._reload_mcp_toolset.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [None, {}, {"server": "weave-connectors"}, [], "ignored"])
async def test_original_reload_ignores_body_and_keeps_full_reload(body):
    runner = _make_bare_runner()
    req = _make_request()
    req.can_read_body = body is not None
    req.json = AsyncMock(side_effect=AssertionError("reload must not parse its body"))
    with (patch("tools.mcp_tool.shutdown_mcp_servers") as shutdown,
          patch("tools.mcp_tool.discover_mcp_tools", return_value=[]) as discover):
        response = await _make_adapter(runner)._handle_mcp_reload(req)
    assert response.status == 200
    assert "schema_refreshed" not in json.loads(response.text)
    shutdown.assert_called_once_with()
    discover.assert_called_once_with()
    req.json.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_refresh", [False, True])
async def test_refresh_and_reload_share_concurrency_guard(first_refresh):
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked(**kwargs):
        entered.set()
        await release.wait()
        return kwargs
    runner = SimpleNamespace(_reload_mcp_toolset=AsyncMock(side_effect=blocked))
    adapter = _make_adapter(runner)
    first_handler = adapter._handle_mcp_refresh if first_refresh else adapter._handle_mcp_reload
    task = asyncio.create_task(first_handler(request()))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert (await adapter._handle_mcp_refresh(request())).status == 409
        assert (await adapter._handle_mcp_reload(request())).status == 409
        assert (await adapter._handle_health(_make_request(method="GET"))).status == 200
    finally:
        release.set()
        response = await task
    assert response.status == 200
    assert not adapter._mcp_reload_in_progress
    runner._reload_mcp_toolset.assert_awaited_once_with(**({"server": "weave-connectors"} if first_refresh else {}))


@pytest.mark.asyncio
async def test_refresh_does_not_shutdown_discover_or_touch_exa():
    runner = _make_bare_runner()
    fresh = [{"type": "function", "function": {"name": "kept", "description": "new schema", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}}]
    from tests.agent.test_turn_context import _FakeAgent, _build
    agent = _FakeAgent()
    stale = [{"type": "function", "function": {"name": "kept", "description": "old", "parameters": {"type": "object"}}}]
    agent.tools = stale
    agent.valid_tool_names = {"kept"}
    agent.enabled_toolsets = ["safe"]
    agent.disabled_toolsets = ["terminal"]
    runner._agent_cache["restricted"] = agent
    with (patch("tools.mcp_tool.refresh_mcp_server_tools", return_value=["fresh"]) as refresh,
          patch("tools.mcp_tool.shutdown_mcp_servers") as shutdown,
          patch("tools.mcp_tool.discover_mcp_tools") as discover,
          patch("model_tools.get_tool_definitions", return_value=fresh) as definitions):
        response = await _make_adapter(runner)._handle_mcp_refresh(request())
        assert agent.tools is stale, "scoped refresh must not mutate an active snapshot"
        with patch("tools.mcp_tool.has_registered_mcp_tools", return_value=True):
            _build(agent)
    assert response.status == 200
    assert json.loads(response.text)["schema_refreshed"] == "weave-connectors"
    assert agent.tools == fresh
    assert agent.valid_tool_names == {"kept"}
    refresh.assert_called_once_with("weave-connectors")
    shutdown.assert_not_called()
    discover.assert_not_called()
    assert definitions.call_args.kwargs["enabled_toolsets"] == ["safe"]
    assert definitions.call_args.kwargs["disabled_toolsets"] == ["terminal"]


@pytest.mark.asyncio
async def test_failure_clears_concurrency_guard():
    runner = SimpleNamespace(_reload_mcp_toolset=AsyncMock(side_effect=RuntimeError("unavailable")))
    adapter = _make_adapter(runner)
    assert (await adapter._handle_mcp_refresh(request())).status == 500
    assert not adapter._mcp_reload_in_progress
    adapter._mcp_reload_in_progress = True
    assert (await adapter._handle_mcp_refresh(request())).status == 409


@pytest.mark.asyncio
async def test_scoped_auth_precedes_body_parsing_and_malformed_is_rejected():
    runner = SimpleNamespace(_reload_mcp_toolset=AsyncMock())
    adapter = _make_adapter(runner)
    req = request(token=None)
    assert (await adapter._handle_mcp_refresh(req)).status == 401
    req.json.assert_not_called()
    assert (await adapter._handle_mcp_refresh(_make_request())).status == 400
    req = request()
    req.json.side_effect = ValueError("invalid JSON")
    assert (await adapter._handle_mcp_refresh(req)).status == 400
    runner._reload_mcp_toolset.assert_not_called()


@pytest.mark.asyncio
async def test_registry_refresh_failure_is_not_scoped_success():
    runner = _make_bare_runner()
    runner._agent_cache["session"] = SimpleNamespace()
    with (patch("tools.mcp_tool.refresh_mcp_server_tools", side_effect=RuntimeError("failed"))):
        assert (await _make_adapter(runner)._handle_mcp_refresh(request())).status == 500


def test_refresh_route_is_distinct_and_no_capability_route():
    routes = {(method, path): handler.__name__ for method, path, handler in _make_adapter()._http_route_table()}
    assert routes[("POST", "/v1/mcp/refresh")] == "_handle_mcp_refresh"
    assert routes[("POST", "/v1/mcp/reload")] == "_handle_mcp_reload"
    assert ("GET", "/v1/mcp/reload") not in routes
    assert ("GET", "/v1/mcp/refresh") not in routes


@pytest.mark.asyncio
async def test_http_routes_preserve_ignored_reload_bodies_and_scoped_refresh():
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    runner = SimpleNamespace(_reload_mcp_toolset=AsyncMock(return_value={"tools": 1}))
    adapter = _make_adapter(runner)
    app = web.Application()
    for method, path, handler in adapter._http_route_table():
        if path.startswith("/v1/mcp/"):
            app.router.add_route(method, path, handler)
    async with TestClient(TestServer(app)) as client:
        headers = {"Authorization": "Bearer " + API_KEY}
        for payload in (b"", b"{}", b'{"server":"weave-connectors"}', b"not-json"):
            response = await client.post("/v1/mcp/reload", headers=headers, data=payload)
            assert response.status == 200
            assert await response.json() == {"tools": 1}
        assert runner._reload_mcp_toolset.await_args_list == [call()] * 4
        runner._reload_mcp_toolset.reset_mock()
        response = await client.get("/v1/mcp/reload", headers=headers)
        assert response.status == 405
        response = await client.post("/v1/mcp/refresh", headers=headers, json={})
        assert response.status == 400
        runner._reload_mcp_toolset.assert_not_called()
        runner._reload_mcp_toolset.return_value = {"schema_refreshed": "weave-connectors"}
        response = await client.post("/v1/mcp/refresh", headers=headers, json={"server": "weave-connectors"})
        assert response.status == 200
        assert await response.json() == {"schema_refreshed": "weave-connectors"}
        runner._reload_mcp_toolset.assert_awaited_once_with(server="weave-connectors")
