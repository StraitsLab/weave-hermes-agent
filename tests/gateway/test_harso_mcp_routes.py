import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.platforms import api_server_harso_mcp as mcp
from gateway.platforms.api_server import _api_request_profile


@pytest.mark.asyncio
async def test_mcp_http_root_auth_and_selector(monkeypatch):
    reload = Mock(return_value={'tools': 1})
    monkeypatch.setattr(mcp, 'reload_tools', reload)
    adapter = SimpleNamespace(_api_key='root', _auth_failed_response=lambda: web.Response(status=401))
    app = web.Application()
    for method, path, handler in mcp.routes(adapter):
        app.router.add_route(method, path, handler)
    async with TestClient(TestServer(app)) as client:
        for path in ['/v1/mcp/reload', '/v1/mcp/refresh']:
            assert (await client.post(path, json={})).status == 401
            assert (await client.post(path, json={}, headers={'Authorization': 'Bearer session'})).status == 401
        headers = {'Authorization': 'Bearer root'}
        for body in [[], {'other': 1}, {'server': 'other'}, {'server': []}]:
            assert (await client.post('/v1/mcp/reload', json=body, headers=headers)).status == 400
        assert (await client.post('/v1/mcp/refresh', json={}, headers=headers)).status == 400
        assert not reload.called
        assert (await client.post('/v1/mcp/reload', headers=headers)).status == 200
        assert reload.call_args.args == (None,)
        assert (await client.post('/v1/mcp/reload', data='{' , headers=headers)).status == 400
        assert (await client.post('/v1/mcp/reload', data='x' * 4097, headers=headers)).status == 400
        assert (await client.post('/v1/mcp/reload', json={}, headers=headers)).status == 200
        assert reload.call_args.args == (None,)
        response = await client.post('/v1/mcp/refresh', json={'server': 'weave-connectors'}, headers=headers)
        assert response.status == 200
        assert (await response.json())['tools'] == 1
        assert reload.call_args.args == ('weave-connectors',)
        reload.side_effect = RuntimeError('secret-token')
        response = await client.post('/v1/mcp/reload', json={}, headers=headers)
        assert response.status == 503 and 'secret-token' not in await response.text()


@pytest.mark.asyncio
async def test_reload_exclusion_survives_request_cancellation(monkeypatch):
    import asyncio
    import threading
    started, release = threading.Event(), threading.Event()
    def reload(server):
        started.set()
        assert release.wait(10)
        return {'tools': 1}
    monkeypatch.setattr(mcp, 'reload_tools', reload)
    adapter = SimpleNamespace(_api_key='root', _auth_failed_response=lambda: web.Response(status=401))
    class Body:
        async def read(self, size):
            return b''
    request = SimpleNamespace(headers={'Authorization': 'Bearer root'}, content_length=0, content=Body())
    task = asyncio.create_task(mcp.change(adapter, request, refresh=False))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        assert (await mcp.change(adapter, request, refresh=False)).status == 409
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert adapter._mcp_reload_in_progress
        assert (await mcp.change(adapter, request, refresh=False)).status == 409
    finally:
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and
                   'perform' in t.get_coro().__qualname__]
        release.set()
        await asyncio.gather(*pending)
    assert not adapter._mcp_reload_in_progress


def test_profile_url_is_never_a_root_control_route():
    import asyncio
    token = _api_request_profile.set('work')
    try:
        adapter = SimpleNamespace(_api_key='root', _auth_failed_response=lambda: web.Response(status=401))
        request = SimpleNamespace(headers={'Authorization': 'Bearer root'})
        assert asyncio.run(mcp.change(adapter, request, refresh=False)).status == 401
    finally:
        _api_request_profile.reset(token)
