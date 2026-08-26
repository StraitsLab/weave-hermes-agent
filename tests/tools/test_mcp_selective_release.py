"""Session-scoped ownership for ACP-supplied MCP transports."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools import mcp_tool

_STATE_NAMES = ("_session_mcp_fingerprints", "_session_mcp_owners",
                "_session_mcp_managed", "_session_mcp_releasing")


class _Server:
    def __init__(self, config, *, fail_shutdown=False):
        self._config = dict(config)
        self.session = object()
        self._registered_tool_names = []
        self.fail_shutdown = fail_shutdown
        self.shutdown_calls = 0

    async def shutdown(self):
        self.shutdown_calls += 1
        if self.fail_shutdown:
            raise RuntimeError("secret shutdown detail")


@pytest.fixture(autouse=True)
def _isolated_session_ownership():
    saved = dict(mcp_tool._servers)
    mcp_tool._servers.clear()
    for name in _STATE_NAMES:
        getattr(mcp_tool, name).clear()
    yield
    mcp_tool._servers.clear()
    mcp_tool._servers.update(saved)
    for name in _STATE_NAMES:
        getattr(mcp_tool, name).clear()


def _run_here(coro_or_factory, timeout=30):
    coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
    return asyncio.run(coro)


def _config(*, port=43123, secret="first-secret", name="attempt"):
    return {name: {"url": f"http://127.0.0.1:{port}/mcp",
                   "headers": {"Authorization": f"Bearer {secret}"}}}


def _register(name, server):
    def register(requested):
        mcp_tool._servers[name] = server
        return []
    return register


def test_identical_descriptor_is_shared_until_last_owner_releases():
    config = _config()
    server = _Server(config["attempt"])
    with patch.object(mcp_tool, "register_mcp_servers", side_effect=_register("attempt", server)), patch.object(
        mcp_tool, "_run_on_mcp_loop", side_effect=_run_here
    ):
        mcp_tool.acquire_session_mcp_servers("session-a", config)
        mcp_tool.acquire_session_mcp_servers("session-b", config)
        mcp_tool.release_session_mcp_servers("session-a")
        assert server.shutdown_calls == 0
        assert "attempt" in mcp_tool._servers
        mcp_tool.release_session_mcp_servers("session-b")
    assert server.shutdown_calls == 1
    assert "attempt" not in mcp_tool._servers


def test_same_name_with_different_descriptor_fails_without_secret_text():
    first = _config()
    second = _config(port=43124, secret="second-secret")
    server = _Server(first["attempt"])
    with patch.object(mcp_tool, "register_mcp_servers", side_effect=_register("attempt", server)):
        mcp_tool.acquire_session_mcp_servers("session-a", first)
        with pytest.raises(RuntimeError) as raised:
            mcp_tool.acquire_session_mcp_servers("session-b", second)

    message = str(raised.value)
    assert "first-secret" not in message
    assert "second-secret" not in message
    assert mcp_tool._session_mcp_owners["attempt"] == {"session-a"}


def test_failed_last_owner_release_is_retryable():
    config = {"attempt": {"url": "https://example.test/mcp", "headers": {}}}
    server = _Server(config["attempt"], fail_shutdown=True)
    with patch.object(mcp_tool, "register_mcp_servers", side_effect=_register("attempt", server)), patch.object(
        mcp_tool, "_run_on_mcp_loop", side_effect=_run_here
    ):
        mcp_tool.acquire_session_mcp_servers("session-a", config)
        with pytest.raises(RuntimeError, match="release failed") as raised:
            mcp_tool.release_session_mcp_servers("session-a")
        assert "secret shutdown detail" not in str(raised.value)
        assert mcp_tool._session_mcp_owners["attempt"] == {"session-a"}
        assert mcp_tool._servers["attempt"] is server
        server.fail_shutdown = False
        mcp_tool.release_session_mcp_servers("session-a")
    assert "attempt" not in mcp_tool._servers


def test_preexisting_identical_server_is_borrowed_not_shutdown():
    config = {"shared": {"url": "https://example.test/mcp", "headers": {}}}
    server = _Server(config["shared"])
    mcp_tool._servers["shared"] = server

    with patch.object(mcp_tool, "register_mcp_servers") as register:
        mcp_tool.acquire_session_mcp_servers("session-a", config)
        mcp_tool.release_session_mcp_servers("session-a")

    register.assert_not_called()
    assert server.shutdown_calls == 0
    assert mcp_tool._servers["shared"] is server


def test_full_shutdown_clears_transient_ownership_without_live_servers():
    mcp_tool._session_mcp_fingerprints["attempt"] = "digest"
    mcp_tool._session_mcp_owners["attempt"] = {"session-a"}
    mcp_tool._session_mcp_managed.add("attempt")

    with patch.object(mcp_tool, "_stop_mcp_loop"):
        mcp_tool.shutdown_mcp_servers()

    assert mcp_tool._session_mcp_fingerprints == {}
    assert mcp_tool._session_mcp_owners == {}
    assert mcp_tool._session_mcp_managed == set()


def test_full_shutdown_timeout_still_clears_transient_ownership():
    server = _Server({})
    mcp_tool._servers["attempt"] = server
    mcp_tool._session_mcp_fingerprints["attempt"] = "digest"
    mcp_tool._session_mcp_owners["attempt"] = {"session-a"}
    mcp_tool._session_mcp_managed.add("attempt")

    class _Future:
        def result(self, timeout):
            raise TimeoutError

    def schedule(coro, loop, **kwargs):
        coro.close()
        return _Future()

    loop = SimpleNamespace(is_running=lambda: True)
    with patch.object(mcp_tool, "_mcp_loop", loop), patch(
        "agent.async_utils.safe_schedule_threadsafe", side_effect=schedule
    ), patch.object(mcp_tool, "_stop_mcp_loop"):
        mcp_tool.shutdown_mcp_servers()

    assert mcp_tool._session_mcp_fingerprints == {}
    assert mcp_tool._session_mcp_owners == {}


def test_http_session_support_requires_new_strict_redirect_transport():
    with patch.object(mcp_tool, "_ensure_mcp_sdk", return_value=True), patch.object(
        mcp_tool, "_MCP_HTTP_AVAILABLE", True
    ), patch.object(mcp_tool, "_MCP_NEW_HTTP", False):
        assert mcp_tool.supports_session_http_mcp() is False
