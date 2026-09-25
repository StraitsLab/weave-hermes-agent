"""Live-view bridge: the Work attempt's Bot Screen over two loopback WebSockets for weave-api.

``WS /display`` pumps raw RFB between one viewer and Xvnc, gated by the lease (a copy of upstream
``hermes_cli/web_routers/display.py`` ``_bridge``/``_should_evict`` @ ee5ee84a; that module is not imported,
its dashboard-auth import does not exist in the fork). ``WS /control`` takes one JSON request and returns one
JSON reply: ``{op:'lease', action:'take'|'release'|'status', viewer_id, force?}`` or
``{op:'type', viewer_id, text}``. There is no HTTP ``/lease`` or ``/type`` route: one lease entry point.

Auth: every route requires ``Authorization: HMAC <secret>``, compared in constant time; a failure closes
4401 after accept (so the code reaches the client) and before any RFB byte. The viewer id is the
``X-Weave-Viewer`` header weave-api sets. The socket is ``$HERMES_BD_SOCKET`` (the variable launcher.sh
gives Xvnc), read once at start. Nothing here logs a frame, a body or a header value.
Recorded hunks against upstream are listed in ``tools/bot_desktop/UPSTREAM.md``.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import socket
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from hermes_constants import get_hermes_home, hermes_home_key

if TYPE_CHECKING:
    from starlette.applications import Starlette
    from starlette.websockets import WebSocket

_log = logging.getLogger(__name__)

# Upstream display.py constants.
_ACTIVITY_STAMP_S = 60.0
_READ_CHUNK = 64 * 1024
_CLOSE_CONTROL_TAKEN = 4000
_CLEAN_CLOSE = frozenset({1000, 1001})
_CLOSE_DESKTOP_GONE = 4001
_CLOSE_BAD_AUTH = 4401
_CLOSE_PROTOCOL = 1003
_LEASE_REFRESH_S = 0.25
# Bridge-only.
_CLOSE_NO_VIEWER = 4400
_MAX_TYPE_CHARS = 1024
_RFB_TIMEOUT_S = 5.0


def _should_evict(held: dict, lease, viewer_id: str) -> bool:
    """Upstream verbatim: a viewer that held control during this connection and lost it to ANOTHER human
    is kicked so its UI repaints; a hand-back forgets that it ever held."""
    from tools.bot_desktop import lease as _lease
    if lease.holder != _lease.HUMAN:
        held["ever"] = False
        return False
    if lease.viewer_id == viewer_id:
        held["ever"] = True
        return False
    return bool(held["ever"])


def socket_path_from_env() -> Path:
    raw = os.environ.get("HERMES_BD_SOCKET", "")
    if not raw or not os.path.isabs(raw):
        raise ValueError("HERMES_BD_SOCKET must be set to an absolute path")
    return Path(raw)


def create_app(*, secret: str, socket_path: Optional[Path] = None) -> Starlette:
    # Lazy: the CLI parser imports this module. Plain Starlette routes (FastAPI's base): no annotation-driven
    # parameter parsing, and any HTTP path (``POST /lease``, ``POST /type``) is a 404.
    from starlette.applications import Starlette
    from starlette.routing import WebSocketRoute
    from starlette.websockets import WebSocketDisconnect

    if not secret:
        raise ValueError("bridge secret is empty")
    sock = socket_path or socket_path_from_env()
    profile_home = str(get_hermes_home())
    expected = f"HMAC {secret}".encode()
    async def _admit(ws: WebSocket) -> bool:
        await ws.accept()
        got = ws.headers.get("authorization", "").encode()
        if hmac.compare_digest(got, expected):
            return True
        _log.warning("live-view bridge: %s refused, bad credentials", ws.url.path)
        await ws.close(code=_CLOSE_BAD_AUTH, reason="unauthorized")
        return False

    async def display(ws: WebSocket) -> None:
        if not await _admit(ws):
            return
        viewer_id = ws.headers.get("x-weave-viewer", "")
        if not viewer_id:
            await ws.close(code=_CLOSE_NO_VIEWER, reason="viewer id missing")
            return
        await _bridge(ws, sock, profile_home, viewer_id)

    async def control(ws: WebSocket) -> None:
        if not await _admit(ws):
            return
        try:
            raw = await ws.receive_text()
        except (WebSocketDisconnect, KeyError):
            return
        reply = await _control(raw, sock, profile_home)
        _log.info("live-view bridge: control ok=%s error=%s", reply.get("ok"), reply.get("error", "-"))
        await ws.send_text(json.dumps(reply))
        await ws.close()

    return Starlette(routes=[WebSocketRoute("/display", display), WebSocketRoute("/control", control)])


async def _control(raw: str, sock: Path, profile_home: str) -> dict:
    from tools.bot_desktop import lease as _lease
    bad = {"ok": False, "error": "bad_request"}
    try:
        req = json.loads(raw)
    except ValueError:
        return bad
    if not isinstance(req, dict):
        return bad
    viewer_id = req.get("viewer_id")
    if viewer_id is not None and (not isinstance(viewer_id, str) or not viewer_id):
        return bad
    op, action = req.get("op"), req.get("action")

    def _state(lease) -> dict:
        holds = bool(viewer_id) and lease.holder == _lease.HUMAN and lease.viewer_id == viewer_id
        return {"ok": True, "lease": _lease.public_view(lease), "viewer_holds": holds}

    if op == "lease" and action == "status":
        return _state(_lease.get(profile_key=profile_home))
    if op == "lease" and action == "take" and viewer_id:
        return _state(await asyncio.to_thread(_lease.acquire, viewer_id, profile_key=profile_home))
    if op == "lease" and action == "release" and req.get("force") is True:
        return _state(await asyncio.to_thread(_lease.release, None, profile_key=profile_home))
    if op == "lease" and action == "release" and viewer_id:
        return _state(await asyncio.to_thread(_lease.release, viewer_id, profile_key=profile_home))
    if op == "type" and viewer_id and isinstance(req.get("text"), str) and 0 < len(req["text"]) <= _MAX_TYPE_CHARS:
        return await _type_text(sock, profile_home, viewer_id, req["text"])
    return bad


def _keysym(ch: str) -> Optional[int]:
    cp = ord(ch)
    if cp < 0x20 or 0x7F <= cp < 0xA0 or 0xD800 <= cp < 0xE000:
        return None
    return cp if cp <= 0xFF else 0x01000000 + cp


async def _type_text(sock: Path, profile_home: str, viewer_id: str, text: str) -> dict:
    """Type ``text`` as RFB KeyEvent down/up pairs on a short connection of our own; never ClientCutText (the
    X clipboard would keep it). The lease is re-checked before every key, so a takeover stops typing."""
    from tools.bot_desktop import lease as _lease
    maybe = [_keysym(ch) for ch in text]
    syms = [sym for sym in maybe if sym is not None]
    if len(syms) != len(maybe):
        return {"ok": False, "error": "unsupported_text"}
    if not _lease.viewer_may_send_input(viewer_id, profile_key=profile_home):
        return {"ok": False, "error": "not_holder"}
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(sock)), _RFB_TIMEOUT_S)
    except (OSError, asyncio.TimeoutError):
        return {"ok": False, "error": "screen_unavailable"}
    try:
        await asyncio.wait_for(_rfb_handshake(reader, writer), _RFB_TIMEOUT_S)
        for sym in syms:
            if not _lease.viewer_may_send_input(viewer_id, profile_key=profile_home):
                return {"ok": False, "error": "not_holder"}
            writer.write(bytes([4, 1, 0, 0]) + sym.to_bytes(4, "big") + bytes([4, 0, 0, 0]) + sym.to_bytes(4, "big"))
            await writer.drain()
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError):
        return {"ok": False, "error": "screen_unavailable"}
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
    return {"ok": True, "typed": len(syms)}


async def _rfb_handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """RFB 3.8 client side against Xvnc ``-SecurityTypes None``, shared session, up to the message phase."""
    if not (await reader.readexactly(12)).startswith(b"RFB 003."):
        raise ValueError("not an RFB server")
    writer.write(b"RFB 003.008\n")
    count = (await reader.readexactly(1))[0]
    if 1 not in await reader.readexactly(count):
        raise ValueError("security type None not offered")
    writer.write(b"\x01")
    if await reader.readexactly(4) != b"\x00\x00\x00\x00":
        raise ValueError("security handshake failed")
    writer.write(b"\x01")  # ClientInit: shared
    server_init = await reader.readexactly(24)
    await reader.readexactly(int.from_bytes(server_init[20:24], "big"))


async def _bridge(ws: WebSocket, sock: Path, profile_home: str, viewer_id: str) -> None:
    """Upstream ``_bridge`` with recorded hunks: the socket is the explicit ``sock`` (not
    ``<HERMES_HOME>/bot-desktop/rfb.sock``), the activity stamp sits beside it, and the viewer id is an
    argument. Pump RFB bytes between the accepted viewer socket and Xvnc, gated by the lease."""
    from starlette.websockets import WebSocketDisconnect

    from tools.bot_desktop import lease as _lease
    from tools.bot_desktop.rfb_filter import RfbClientFilter

    profile_key = hermes_home_key(profile_home)
    if not sock.exists():
        await ws.close(code=_CLOSE_DESKTOP_GONE, reason="Bot Desktop is not running")
        return
    try:
        reader, writer = await asyncio.open_unix_connection(str(sock))
    except OSError:
        await ws.close(code=_CLOSE_DESKTOP_GONE, reason="Bot Desktop socket unreachable")
        return

    loop = asyncio.get_running_loop()
    evicted = asyncio.Event()
    held = {"ever": _lease.viewer_may_send_input(viewer_id, profile_key=profile_home)}
    allowed = {"input": held["ever"], "at": loop.time()}

    def _refresh_allowed(lease=None) -> None:
        if lease is None:
            lease = _lease.get(profile_key=profile_home)
        allowed["input"] = lease.holder == _lease.HUMAN and lease.viewer_id == viewer_id
        allowed["at"] = loop.time()

    def _may_send_input() -> bool:
        if loop.time() - allowed["at"] > _LEASE_REFRESH_S:
            _refresh_allowed()
        return bool(allowed["input"])

    def _on_lease(key: str, lease) -> None:
        if key != profile_key:
            return
        loop.call_soon_threadsafe(_refresh_allowed, lease)
        if _should_evict(held, lease, viewer_id):
            loop.call_soon_threadsafe(evicted.set)
    unsubscribe = _lease.on_change(_on_lease)

    rfb_filter = RfbClientFilter(_may_send_input)

    viewer_closed = asyncio.Event()
    activity_file = sock.parent / "activity"
    stamped = {"at": 0.0}

    def _stamp_activity() -> None:
        if loop.time() - stamped["at"] < _ACTIVITY_STAMP_S:
            return
        stamped["at"] = loop.time()
        try:
            activity_file.touch()
        except OSError:
            pass

    async def rfb_to_ws() -> None:
        while True:
            chunk = await reader.read(_READ_CHUNK)
            if not chunk:
                return
            _stamp_activity()
            await ws.send_bytes(chunk)

    async def ws_to_rfb() -> None:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                if message.get("code") in _CLEAN_CLOSE:
                    viewer_closed.set()
                return
            data = message.get("bytes")
            if data is None:
                await ws.close(code=_CLOSE_PROTOCOL, reason="RFB is binary")
                return
            try:
                allowed_bytes = rfb_filter.feed(data)
            except ValueError as exc:
                await ws.close(code=_CLOSE_PROTOCOL, reason=str(exc)[:100])
                return
            if allowed_bytes:
                writer.write(allowed_bytes)
                await writer.drain()

    async def watch_eviction() -> None:
        await evicted.wait()
        await ws.close(code=_CLOSE_CONTROL_TAKEN, reason="control-taken")

    tasks = [asyncio.create_task(rfb_to_ws()), asyncio.create_task(ws_to_rfb()),
             asyncio.create_task(watch_eviction())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for t in done:
            exc = t.exception()
            if exc and not isinstance(exc, (WebSocketDisconnect, ConnectionError)):
                _log.debug("live-view display ended: %s", type(exc).__name__)
    finally:
        unsubscribe()
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        # A clean viewer close hands control back; a dropped link keeps the human's exclusion.
        if viewer_closed.is_set() and _lease.viewer_may_send_input(viewer_id, profile_key=profile_home):
            _lease.release(viewer_id, profile_key=profile_home)
        try:
            await ws.close()
        except Exception:
            pass


def bind_loopback(listen: str) -> socket.socket:
    """``127.0.0.1:<port>`` only: the bridge is reached through the Sprites tunnel, never a routable address."""
    host, sep, port = listen.rpartition(":")
    if not sep or host != "127.0.0.1" or not port.isdigit() or int(port) > 65535:
        raise ValueError("--listen must be 127.0.0.1:<port>")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((host, int(port)))
    sock.listen(64)
    return sock


def write_port_file(path: str, port: int) -> None:
    """0600 whatever the umask, and atomic: the attempt host polls for the file and reads it once."""
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, str(port).encode())
    finally:
        os.close(fd)
    os.replace(tmp, path)


def run_bridge(listen: str, secret_file: str, port_file: str) -> int:
    import sys

    import uvicorn
    try:
        secret = Path(secret_file).read_text(encoding="utf-8").strip()
        app = create_app(secret=secret)
        sock = bind_loopback(listen)
    except (OSError, ValueError) as exc:
        print(f"live-view bridge: {exc}", file=sys.stderr)
        return 2
    write_port_file(port_file, sock.getsockname()[1])
    config = uvicorn.Config(app, log_level="warning", access_log=False, lifespan="off")
    uvicorn.Server(config).run(sockets=[sock])
    return 0


def build_screen_parser(computer_use_sub) -> None:
    screen = computer_use_sub.add_parser("screen", help="Bot Screen live-view bridge (Work attempts)")
    sub = screen.add_subparsers(dest="computer_use_screen_action")
    br = sub.add_parser("bridge", help="Serve /display and /control on loopback for weave-api")
    br.add_argument("--listen", default="127.0.0.1:0", help="127.0.0.1:<port> (0 = any free port)")
    br.add_argument("--secret-file", required=True, help="File holding the HMAC bridge secret")
    br.add_argument("--port-file", required=True, help="Written 0600 with the bound port")

    def _cmd(args):
        if getattr(args, "computer_use_screen_action", None) == "bridge":
            return run_bridge(args.listen, args.secret_file, args.port_file)
        screen.print_help()
        return 1
    screen.set_defaults(screen_func=_cmd)
