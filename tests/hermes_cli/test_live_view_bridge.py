"""LV-1b live-view bridge: /display (RFB pump) and /control (lease + type), HMAC auth (T-1b-1..7).

Each test names the mutant it must turn red (live-view ENGINEERING-PLAN v6 §5)."""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hermes_cli import live_view_bridge as bridge
from tools.bot_desktop import lease

SECRET = "b" * 64


def _auth() -> dict:
    # A fresh dict per call: starlette's TestClient.websocket_connect setdefault()s handshake keys into it.
    return {"Authorization": f"HMAC {SECRET}"}

_CLIENT_HANDSHAKE = b"RFB 003.008\n" + b"\x01" + b"\x00"  # version, security None, ClientInit(exclusive)
_FORWARDED_HANDSHAKE = b"RFB 003.008\n" + b"\x01" + b"\x01"  # the filter forces the shared flag
_KEY = struct.pack(">BBxxI", 4, 1, 0x61)
_POINTER = struct.pack(">BBHH", 5, 1, 10, 10)
_CUT = struct.pack(">BxxxI", 6, 3) + b"abc"
_FUR = struct.pack(">BBHHHH", 3, 1, 0, 0, 64, 64)


def _recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("short read")
        buf += chunk
    return buf


class FakeRfb:
    """A Unix-socket RFB server (Xvnc stand-in). ``handshake`` completes the 3.8 server side so a client
    can reach the message phase; ``received`` holds the client bytes after the handshake (or all bytes)."""

    def __init__(self, path: str, handshake: bool = False) -> None:
        self.path, self.handshake = path, handshake
        self.received = bytearray()
        self.connections = 0
        self.done = threading.Event()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(path)
        self._sock.listen(4)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.connections += 1
            with conn:
                conn.sendall(b"RFB 003.008\n")
                if self.handshake:
                    _recv_exact(conn, 12)
                    conn.sendall(b"\x01\x01")  # one security type: None
                    _recv_exact(conn, 1)
                    conn.sendall(b"\x00\x00\x00\x00")  # SecurityResult OK
                    _recv_exact(conn, 1)  # ClientInit
                    conn.sendall(bytes(20) + (4).to_bytes(4, "big") + b"test")  # ServerInit
                while True:
                    try:
                        chunk = conn.recv(65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    self.received += chunk
            self.done.set()

    def close(self) -> None:
        self._sock.close()


@pytest.fixture
def screen(monkeypatch):
    """A short private state dir (AF_UNIX limit) with ``HERMES_BD_SOCKET`` pointing into it."""
    d = tempfile.mkdtemp(prefix="bd", dir="/tmp")
    sock = os.path.join(d, "rfb.sock")
    monkeypatch.setenv("HERMES_BD_SOCKET", sock)
    lease._reset_for_tests()
    yield sock
    lease._reset_for_tests()
    shutil.rmtree(d, ignore_errors=True)


def _client() -> TestClient:
    return TestClient(bridge.create_app(secret=SECRET))


def _control(client: TestClient, body: dict, headers: dict | None = None) -> dict:
    with client.websocket_connect("/control", headers=_auth() if headers is None else dict(headers)) as ws:
        ws.send_text(json.dumps(body))
        return json.loads(ws.receive_text())


def _pump(client: TestClient, server: FakeRfb, viewer: str, payload: bytes) -> bytes:
    with client.websocket_connect("/display", headers={**_auth(), "X-Weave-Viewer": viewer}) as ws:
        banner = ws.receive_bytes()
        ws.send_bytes(payload)
        ws.close(1000)
    assert server.done.wait(5)
    return banner


# T-1b-1 — mutant: replace hmac.compare_digest with True
@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "HMAC " + "a" * 64},
    {"Authorization": f"Bearer {SECRET}"},
    {"Authorization": f"HMAC {SECRET}x"},
])
@pytest.mark.parametrize("route", ["/display", "/control"])
def test_bad_or_missing_hmac_closes_4401_before_any_rfb_byte(screen, headers, route):
    server = FakeRfb(screen)
    try:
        with _client().websocket_connect(route, headers={**headers, "X-Weave-Viewer": "v1"}) as ws:
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_bytes()
        assert closed.value.code == 4401
        time.sleep(0.1)
        assert server.connections == 0
    finally:
        server.close()


# T-1b-2 — mutant: bypass RfbClientFilter
def test_a_watchers_input_never_reaches_the_socket(screen):
    server = FakeRfb(screen)
    try:
        banner = _pump(_client(), server, "watcher", _CLIENT_HANDSHAKE + _KEY + _POINTER + _CUT + _FUR)
    finally:
        server.close()
    assert banner == b"RFB 003.008\n"
    assert bytes(server.received) == _FORWARDED_HANDSHAKE + _FUR


def test_the_lease_holders_input_does_reach_the_socket(screen):
    lease.acquire("holder", profile_key=str(bridge.get_hermes_home()))
    server = FakeRfb(screen)
    try:
        _pump(_client(), server, "holder", _CLIENT_HANDSHAKE + _KEY + _POINTER + _FUR)
    finally:
        server.close()
    assert bytes(server.received) == _FORWARDED_HANDSHAKE + _KEY + _POINTER + _FUR


# T-1b-3 — mutants: release ignores viewer_id; re-add a POST route
def test_control_lease_round_trips_through_the_lease_file(screen):
    client = _client()
    home = str(bridge.get_hermes_home())
    took = _control(client, {"op": "lease", "action": "take", "viewer_id": "desk-1"})
    assert took["ok"] and took["viewer_holds"] and lease.get(profile_key=home).viewer_id == "desk-1"

    _control(client, {"op": "lease", "action": "take", "viewer_id": "desk-2"})  # last writer wins
    stale = _control(client, {"op": "lease", "action": "release", "viewer_id": "desk-1"})
    assert stale["viewer_holds"] is False
    assert lease.get(profile_key=home).viewer_id == "desk-2", "a non-holder's release must not yank control"

    status = _control(client, {"op": "lease", "action": "status", "viewer_id": "desk-2"})
    assert status["viewer_holds"] and status["lease"]["holder"] == lease.HUMAN
    assert "desk-2" not in json.dumps(status["lease"]), "the holder's id is a capability; only its hash is public"

    own = _control(client, {"op": "lease", "action": "release", "viewer_id": "desk-2"})
    assert own["lease"]["holder"] == lease.AGENT

    _control(client, {"op": "lease", "action": "take", "viewer_id": "desk-3"})
    forced = _control(client, {"op": "lease", "action": "release", "force": True})
    assert forced["ok"] and lease.get(profile_key=home).holder == lease.AGENT


def test_there_is_no_http_lease_or_type_route(screen):
    client = _client()
    for path in ("/lease", "/type"):
        assert client.post(path, headers=_auth(), json={}).status_code == 404


@pytest.mark.parametrize("body", [
    "not json", json.dumps([1]), json.dumps({"op": "lease", "action": "steal", "viewer_id": "v"}),
    json.dumps({"op": "lease", "action": "take"}), json.dumps({"op": "nope"}),
    json.dumps({"op": "type", "viewer_id": "v", "text": ""}),
])
def test_control_rejects_malformed_requests(screen, body):
    with _client().websocket_connect("/control", headers=_auth()) as ws:
        ws.send_text(body)
        reply = json.loads(ws.receive_text())
    assert reply == {"ok": False, "error": "bad_request"}


# T-1b-4 — mutants: send via ClientCutText; drop the lease check
def test_type_refuses_a_viewer_without_the_lease(screen):
    server = FakeRfb(screen, handshake=True)
    try:
        lease.acquire("someone-else", profile_key=str(bridge.get_hermes_home()))
        reply = _control(_client(), {"op": "type", "viewer_id": "desk-1", "text": "hunter2"})
        time.sleep(0.1)
    finally:
        server.close()
    assert reply == {"ok": False, "error": "not_holder"}
    assert server.connections == 0


def test_type_sends_only_keyevent_pairs(screen):
    text = "pä€ss"
    server = FakeRfb(screen, handshake=True)
    try:
        lease.acquire("desk-1", profile_key=str(bridge.get_hermes_home()))
        reply = _control(_client(), {"op": "type", "viewer_id": "desk-1", "text": text})
        assert server.done.wait(5)
    finally:
        server.close()
    assert reply == {"ok": True, "typed": len(text)}
    got = bytes(server.received)
    assert len(got) == 16 * len(text)
    messages = [struct.unpack(">BBxxI", got[i:i + 8]) for i in range(0, len(got), 8)]
    assert {m[0] for m in messages} == {4}, "only KeyEvent (type 4), never ClientCutText (6)"
    expected = []
    for ch in text:
        sym = ord(ch) if ord(ch) <= 0xFF else 0x01000000 + ord(ch)
        expected += [(4, 1, sym), (4, 0, sym)]
    assert messages == expected


def test_type_refuses_control_characters(screen):
    lease.acquire("desk-1", profile_key=str(bridge.get_hermes_home()))
    reply = _control(_client(), {"op": "type", "viewer_id": "desk-1", "text": "a\nb"})
    assert reply == {"ok": False, "error": "unsupported_text"}


# T-1b-5 — mutant: log the request body
def test_no_frame_body_or_header_value_in_logs(screen, caplog):
    caplog.set_level(logging.DEBUG)
    typed = "Correct-Horse-9"
    server = FakeRfb(screen, handshake=True)
    try:
        client = _client()
        _control(client, {"op": "lease", "action": "take", "viewer_id": "desk-1"})
        _control(client, {"op": "type", "viewer_id": "desk-1", "text": typed})
        _control(client, {"op": "type", "viewer_id": "desk-9", "text": typed})
        with client.websocket_connect("/control", headers={"Authorization": "HMAC wrong"}) as ws:
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()
    finally:
        server.close()
    assert caplog.records, "the scan must see the bridge's own log records"
    for secret in (SECRET, typed, "HMAC wrong"):
        assert secret not in caplog.text


# R1 (review t_33313d00) — mutant: drop the bridge's redaction of the lease module's log arguments
_STALE = "cf328dc1b31f4a18b8fd973a1094209c"


def _stale_release(control) -> None:
    for action, viewer in [("take", _STALE), ("take", "new-owner"), ("release", _STALE)]:
        assert control({"op": "lease", "action": action, "viewer_id": viewer})["ok"]


def test_a_stale_release_does_not_log_the_releasers_viewer_id(screen, caplog):
    caplog.set_level(logging.DEBUG)
    client = _client()
    _stale_release(lambda body: _control(client, body))
    assert lease.get(profile_key=str(bridge.get_hermes_home())).viewer_id == "new-owner"
    assert any(r.name == "tools.bot_desktop.lease" for r in caplog.records), "the lease's own trace still lands"
    assert _STALE not in caplog.text


# T-1b-6 — mutant: bind 0.0.0.0
def test_binds_loopback_only_and_refuses_any_other_host():
    sock = bridge.bind_loopback("127.0.0.1:0")
    try:
        assert sock.getsockname()[0] == "127.0.0.1"
    finally:
        sock.close()
    for listen in ("0.0.0.0:0", "localhost:0", ":0", "127.0.0.1"):
        with pytest.raises(ValueError):
            bridge.bind_loopback(listen)


def test_port_file_is_written_0600_whatever_the_umask(tmp_path):
    old = os.umask(0)
    try:
        path = tmp_path / "bridge.port"
        bridge.write_port_file(str(path), 54321)
    finally:
        os.umask(old)
    assert path.read_text(encoding="utf-8") == "54321"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# T-1b-7 — mutant: derive the socket path from HERMES_HOME (upstream behaviour)
def test_display_pumps_from_exactly_the_hermes_bd_socket_path(screen):
    home_sock = Path(bridge.get_hermes_home()) / "bot-desktop" / "rfb.sock"
    assert not home_sock.exists()
    server = FakeRfb(screen)
    try:
        banner = _pump(_client(), server, "watcher", _CLIENT_HANDSHAKE)
    finally:
        server.close()
    assert banner == b"RFB 003.008\n"
    assert server.connections == 1
    assert not home_sock.exists()


@pytest.mark.parametrize("value", [None, "", "tmp/bd/rfb.sock"])
def test_bridge_refuses_to_start_without_an_absolute_socket_path(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("HERMES_BD_SOCKET", raising=False)
    else:
        monkeypatch.setenv("HERMES_BD_SOCKET", value)
    with pytest.raises(ValueError):
        bridge.create_app(secret=SECRET)


def test_display_closes_4001_while_the_screen_is_absent(screen):
    with _client().websocket_connect("/display", headers={**_auth(), "X-Weave-Viewer": "v"}) as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_bytes()
    assert closed.value.code == 4001


# End to end through the real CLI: `hermes computer-use screen bridge`.
def _cli(tmp: Path, env: dict) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "hermes_cli.main", "computer-use", "screen", "bridge", "--listen", "127.0.0.1:0",
         "--secret-file", str(tmp / "bridge.key"), "--port-file", str(tmp / "bridge.port")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_cli_bridge_serves_control_on_the_port_it_writes(screen):
    from websockets.sync.client import connect

    tmp = Path(screen).parent
    (tmp / "bridge.key").write_text(SECRET + "\n", encoding="utf-8")
    proc = _cli(tmp, {**os.environ, "HERMES_BD_SOCKET": screen})
    try:
        port_file = tmp / "bridge.port"
        deadline = time.time() + 30
        while not port_file.exists() and proc.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        assert port_file.exists(), proc.stderr.read() if proc.poll() is not None else "no port file"
        assert stat.S_IMODE(port_file.stat().st_mode) == 0o600
        port = int(port_file.read_text(encoding="utf-8"))
        with connect(f"ws://127.0.0.1:{port}/control", additional_headers=_auth(), open_timeout=10) as ws:
            ws.send(json.dumps({"op": "lease", "action": "status"}))
            reply = json.loads(ws.recv(timeout=10))
        assert reply["ok"] and reply["lease"]["holder"] == lease.AGENT
    finally:
        proc.terminate()
        proc.wait(10)


def test_cli_stale_release_leaves_no_viewer_id_in_agent_log(screen):
    from websockets.sync.client import connect

    tmp = Path(screen).parent
    home = tmp / "home"
    home.mkdir()
    (tmp / "bridge.key").write_text(SECRET, encoding="utf-8")
    proc = _cli(tmp, {**os.environ, "HERMES_HOME": str(home), "HERMES_BD_SOCKET": screen})
    try:
        port_file = tmp / "bridge.port"
        deadline = time.time() + 30
        while not port_file.exists() and proc.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        assert port_file.exists(), proc.stderr.read() if proc.poll() is not None else "no port file"
        port = int(port_file.read_text(encoding="utf-8"))

        def control(body: dict) -> dict:
            with connect(f"ws://127.0.0.1:{port}/control", additional_headers=_auth(), open_timeout=10) as ws:
                ws.send(json.dumps(body))
                return json.loads(ws.recv(timeout=10))
        _stale_release(control)
    finally:
        proc.terminate()
        proc.wait(10)
    log = (home / "logs" / "agent.log").read_text(encoding="utf-8")
    assert "another viewer holds" in log, "the scan must see the lease module's stale-release record"
    assert _STALE not in log


def test_cli_bridge_refuses_to_start_without_the_socket_env(screen):
    tmp = Path(screen).parent
    (tmp / "bridge.key").write_text(SECRET, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "HERMES_BD_SOCKET"}
    proc = _cli(tmp, env)
    assert proc.wait(30) != 0
    assert not (tmp / "bridge.port").exists()
