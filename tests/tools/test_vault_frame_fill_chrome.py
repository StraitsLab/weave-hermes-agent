"""Vault fill reaches a sign-in form inside a same-origin frame and never a cross-origin one.

Real headless Chrome over CDP: runs the exact inspection and fill scripts the vault tool evaluates.
Skipped when Chrome is not installed.
"""
import asyncio
import http.server
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request

import pytest

from agent.vault_login_classifier import (LoginControl, build_fill_js, build_inspection_js,
                                         classify_login_control, select_login_fills)

MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME = shutil.which("google-chrome") or shutil.which("chromium") or (MAC_CHROME if os.path.exists(MAC_CHROME) else None)
pytestmark = pytest.mark.skipif(CHROME is None, reason="Chrome not installed")

FORM = (b'<form><input id="user" type="email" autocomplete="username" aria-label="Email">'
        b'<input id="pw" type="password" autocomplete="current-password" aria-label="Password"></form>')


def _port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def pages():
    port = _port()
    other = f"http://localhost:{port}"  # different origin from 127.0.0.1:port

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            body = {"/": b'<p>top</p><iframe src="/frame" style="width:400px;height:200px"></iframe>'
                         b'<iframe src="' + other.encode() + b'/frame" style="width:400px;height:200px"></iframe>',
                    "/frame": FORM}.get(self.path, b"")
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.fixture
def chrome():
    profile = tempfile.mkdtemp()
    port = _port()
    proc = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                             "--no-first-run", "--site-per-process", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
                ws = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.1)
        yield ws
    finally:
        proc.kill(); proc.wait(); shutil.rmtree(profile, ignore_errors=True)


async def _session(ws_url, url, steps):
    import websockets
    async with websockets.connect(ws_url, max_size=None) as ws:
        n = 0
        async def call(method, **params):
            nonlocal n
            n += 1; await ws.send(json.dumps({"id": n, "method": method, "params": params}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == n:
                    return msg.get("result", {})
        async def ev(expr):
            r = await call("Runtime.evaluate", expression=expr, returnByValue=True)
            return r["result"].get("value")
        await call("Page.navigate", url=url)
        await asyncio.sleep(1.5)
        return await steps(ev)


def test_fill_reaches_same_origin_frame_and_skips_cross_origin(chrome, pages):
    origin = pages

    async def steps(ev):
        nonce = "t1"
        raw = json.loads(await ev(build_inspection_js(nonce)))
        classified = [c for c in (classify_login_control(LoginControl.from_dict(r)) for r in raw) if c]
        fills = select_login_fills(classified, "s3cret-pw", "me@example.com")
        result = json.loads(await ev(build_fill_js(fills, expected_origin=origin, nonce=nonce)))
        same = await ev("(() => { const d = document.querySelectorAll('iframe')[0].contentDocument;"
                        " return [d.getElementById('user').value, d.getElementById('pw').value,"
                        " d.querySelectorAll('[data-hermes-vault-slot]').length]; })()")
        return raw, result, same

    raw, result, same = asyncio.run(_session(chrome, origin + "/", steps))
    assert len(raw) == 2  # only the same-origin frame's two inputs are visible to the vault
    assert result["filled"] == 2
    assert same == ["me@example.com", "s3cret-pw", 0]  # filled, and no stamps left behind


def test_fill_refuses_when_top_origin_differs(chrome, pages):
    async def steps(ev):
        raw = json.loads(await ev(build_inspection_js("t2")))
        fills = [{"index": r["index"], "token": "current-password", "value": "x"} for r in raw if r["type"] == "password"]
        return json.loads(await ev(build_fill_js(fills, expected_origin="https://evil.example", nonce="t2")))

    result = asyncio.run(_session(chrome, pages + "/", steps))
    assert result["refused"] == "origin_changed"
