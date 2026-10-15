"""Explicit Harso MCP schema refresh: reuse a connected transport, never discover."""
from tools import mcp_tool as _core
from tools.mcp_tool_loop import _run_on_mcp_loop
from tools.mcp_tool_scope import _resolve_server_key


def refresh_mcp_server_tools(name):
    # Resolve on the caller thread: the loop thread has no request profile scope.
    key = _resolve_server_key(name)

    async def refresh():
        with _core._lock:
            server = _core._servers.get(key)
        if server is None:
            raise RuntimeError('MCP server is not connected')
        await server._refresh_tools(require_ready=True)
        return list(server._registered_tool_names)

    return _run_on_mcp_loop(refresh, timeout=10)
