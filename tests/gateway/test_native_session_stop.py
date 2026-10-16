"""WEV-3464: POST /api/sessions/{id}/stop ends the running answer and queues nothing."""
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter

SESSION_ID = "weave-stop"


class Agent:
    def __init__(self):
        self.stops = []

    def interrupt(self, message=None):
        self.stops.append(message)


def _adapter(running_agent=None):
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "sk-native-test"}))
    store = SimpleNamespace(_generate_session_key=lambda source: f"key:{source.chat_id}")
    state = SimpleNamespace(turn=SimpleNamespace(agent=running_agent))
    adapter.gateway_runner = SimpleNamespace(
        _running=True, session_store=store, _peek_session_state=lambda key: state if key == f"key:{SESSION_ID}" else None,
        _enqueue_fifo=lambda *args: pytest.fail("stop must not queue a message"),
    )
    return adapter


async def _post(adapter, session_id=SESSION_ID, auth="Bearer sk-native-test"):
    app = web.Application()
    app.router.add_post("/api/sessions/{session_id}/stop", adapter._handle_session_stop)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(f"/api/sessions/{session_id}/stop", headers={"Authorization": auth})
        return response.status, await response.json()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stop_interrupts_the_running_answer():
    agent = Agent()
    status, body = await _post(_adapter(agent))
    assert (status, body) == (200, {"object": "hermes.session.stop", "stopped": True})
    assert len(agent.stops) == 1


@pytest.mark.asyncio
async def test_stop_with_nothing_running_is_a_no_op():
    status, body = await _post(_adapter(None))
    assert (status, body) == (200, {"object": "hermes.session.stop", "stopped": False})


@pytest.mark.asyncio
async def test_stop_needs_the_api_key():
    status, _ = await _post(_adapter(Agent()), auth="Bearer wrong")
    assert status == 401


class HardAgent:
    """Like the real agent: hard_interrupt(message, *, tool_reason) returns None."""

    def __init__(self):
        self.calls = []

    def hard_interrupt(self, message=None, *, tool_reason=None):
        self.calls.append((message, tool_reason))


@pytest.mark.asyncio
async def test_stop_uses_the_hard_interrupt_of_a_real_agent():
    agent = HardAgent()
    status, body = await _post(_adapter(agent))
    assert (status, body) == (200, {"object": "hermes.session.stop", "stopped": True})
    assert agent.calls == [(None, "stopped by the user")]


def test_stop_is_a_registered_harso_route():
    from gateway.platforms import api_server_harso
    routes = api_server_harso._http_routes(_adapter())
    assert ("POST", "/api/sessions/{session_id}/stop") in {(method, path) for method, path, _ in routes}
