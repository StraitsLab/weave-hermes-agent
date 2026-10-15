"""Root-key-only cell MCP control; no session or profile key may reconfigure MCP."""
import asyncio
import hmac
from functools import partial

from aiohttp import web


def routes(adapter):
    return [('POST', '/v1/mcp/reload', partial(change, adapter, refresh=False)),
            ('POST', '/v1/mcp/refresh', partial(change, adapter, refresh=True))]


async def change(adapter, request, *, refresh):
    from gateway.platforms.api_server import _api_request_profile
    root = getattr(adapter, '_api_key', '')
    auth = request.headers.get('Authorization', '')
    token = auth[7:].strip() if auth.startswith('Bearer ') else ''
    if _api_request_profile.get() or not root or not hmac.compare_digest(token.encode(), root.encode()):
        return adapter._auth_failed_response()
    if request.content_length is not None and request.content_length > 4096:
        return web.json_response({'error': 'Invalid MCP request'}, status=400)
    try:
        raw = bytearray()
        while chunk := await request.content.read(4097 - len(raw)):
            raw.extend(chunk)
            if len(raw) > 4096:
                raise ValueError('oversized')
        import json
        body = json.loads(raw) if raw else {}
    except (ValueError, UnicodeDecodeError):
        return web.json_response({'error': 'Invalid JSON body'}, status=400)
    if not isinstance(body, dict) or set(body) - {'server'}:
        return web.json_response({'error': 'Unsupported MCP request fields'}, status=400)
    server = body.get('server')
    if (refresh and server != 'weave-connectors') or (server is not None and server != 'weave-connectors'):
        return web.json_response({'error': 'Unsupported MCP schema refresh selector'}, status=400)
    if getattr(adapter, '_mcp_reload_in_progress', False):
        return web.json_response({'error': 'An MCP reload is already in progress'}, status=409)
    adapter._mcp_reload_in_progress = True

    async def perform():
        try:
            result = await asyncio.to_thread(reload_tools, server)
            return web.json_response({'status': 'ok', **result})
        except Exception:  # health: allow BLE001 -- HTTP boundary: transport errors can carry credentials, so every failure is one opaque 503
            # Transport errors can contain credentials; expose neither exception nor config.
            return web.json_response({'error': 'MCP reload failed'}, status=503)
        finally:
            adapter._mcp_reload_in_progress = False

    # Request cancellation must not release exclusion while its worker is still mutating MCP.
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
        # names also makes scope=None selective instead of a process-wide wildcard.
        shutdown_mcp_servers(scope=scope, names=old)
        with suppress_interactive_oauth():
            tools = discover_mcp_tools()
    with core._lock:
        current = {_key_name(k) for k in core._servers if _key_scope(k) == scope}
    # Never rewrite an active agent's tools; the turn prologue adopts registry content.
    return {**({'schema_refreshed': server} if server else {}), 'added': sorted(current - old),
            'removed': sorted(old - current), 'reconnected': sorted(current & old) if not server else [],
            'tools': len(tools), 'servers': len(current)}
