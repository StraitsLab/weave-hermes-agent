"""Harso MCP routes preserve root authentication and native scoped reloads."""
import asyncio
import uuid
from aiohttp import web


def routes(adapter):
    type(adapter)._handle_mcp_reload = reload
    type(adapter)._handle_mcp_refresh = refresh
    return [('POST', '/v1/mcp/reload', adapter._handle_mcp_reload),
            ('POST', '/v1/mcp/refresh', adapter._handle_mcp_refresh)]


async def reload(adapter, request):
    return await change(adapter, request, refresh=False)


async def refresh(adapter, request):
    return await change(adapter, request, refresh=True)


async def change(adapter, request, *, refresh):
    from gateway.platforms.api_server import _api_request_profile, _openai_error
    auth_error = adapter._check_auth(request)
    if auth_error is not None:
        return auth_error
    if _api_request_profile.get() not in (None, '', 'default'):
        return web.json_response(_openai_error('MCP reload is root-authority only',
                                 code='mcp_reload_forbidden'), status=403)
    if refresh:
        try:
            body = await request.json()
        except Exception:  # health: allow BLE001 -- Malformed input receives a closed HTTP error.
            body = None
        if body != {'server': 'weave-connectors'}:
            return web.json_response(_openai_error('Expected schema refresh selector: weave-connectors',
                                     code='mcp_refresh_invalid_selector'), status=400)
    if getattr(adapter, '_mcp_reload_in_progress', False):
        return web.json_response(_openai_error('An MCP reload is already in progress',
                                 code='mcp_reload_in_progress'), status=409)
    runner = adapter.gateway_runner
    if runner is None:
        return web.json_response(_openai_error('MCP reload unavailable: no gateway runner',
                                 err_type='server_error', code='mcp_reload_unavailable'), status=500)
    adapter._mcp_reload_in_progress = True

    async def perform():
        from gateway import native_awake
        lease = uuid.uuid4().hex
        try:
            if refresh:
                await native_awake.acquire(lease)
            result = (await runner._reload_mcp_toolset(server='weave-connectors') if refresh
                      else await runner._reload_mcp_toolset())
            return web.json_response(result)
        except Exception:  # health: allow BLE001 -- Provider errors may contain credentials.
            return web.json_response(_openai_error('MCP reload failed; see gateway log',
                                     err_type='server_error', code='mcp_reload_failed'), status=500)
        finally:
            adapter._mcp_reload_in_progress = False
            if refresh:
                native_awake.release(lease)

    # Retain exclusion until the native worker finishes after request cancellation.
    return await asyncio.shield(asyncio.create_task(perform()))


def reload_tools(server):
    from tools import mcp_tool as core
    from tools.mcp_tool_scope import _key_name, _key_scope
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    from tools.mcp_tool_discovery import discover_mcp_tools
    from tools.mcp_oauth import suppress_interactive_oauth
    scope = core._mcp_registry_scope()
    with core._lock:
        old = {_key_name(k) for k in core._servers if _key_scope(k) == scope}
    if server is not None:
        from tools.mcp_tool_harso import refresh_mcp_server_tools
        tools = refresh_mcp_server_tools(server)
    else:
        shutdown_mcp_servers(scope=scope, names=old)
        with suppress_interactive_oauth():
            tools = discover_mcp_tools()
    with core._lock:
        current = {_key_name(k) for k in core._servers if _key_scope(k) == scope}
    return {**({'schema_refreshed': server} if server else {}), 'added': sorted(current - old),
            'removed': sorted(old - current), 'reconnected': sorted(current & old) if not server else [],
            'tools': len(tools), 'servers': len(current)}
