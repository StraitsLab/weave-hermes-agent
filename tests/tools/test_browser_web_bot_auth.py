"""Web Bot Auth request signing (fork, AB-2).

The unit tests pin the signature bytes and the fail-open contract. The E2E test drives a real Chrome through the
real ``CDPSupervisor`` against a local server that verifies every request's signature: a navigation, its redirect
hop and a subresource must all arrive signed. It needs ``HERMES_E2E_BROWSER=1`` and a Chrome binary
(``HERMES_E2E_CHROME`` or google-chrome/chromium on PATH).
"""

from __future__ import annotations

import base64
import http.server
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tools import browser_web_bot_auth as wba

AGENT = "https://harso.ai/.well-known/http-message-signatures-directory"

# weave-cloud e3931d448c16 ``infra/web-bot-auth/wba_directory.py sign_request`` with the key bytes(range(32)),
# https://example.com/, created=1700000000, validity 60, nonce "bm9uY2U=".
AB1_SIGN_REQUEST = {
    "Signature": "sig1=:8870Ian/NY1uF0/Zmj3YsfC11GBCYjXG2DxoW5CZHd1ia4MTS4u9r5jsAVxHkTkWjkB9tPdIVi+0dldqzWaEAg==:",
    "Signature-Agent": f'"{AGENT}"',
    "Signature-Input": ('sig1=("@authority" "signature-agent");created=1700000000;'
                        'keyid="1IG2tMH7J2wbJZnOf8LJzQitKf7LMvoAElsuDMVM54Y";alg="ed25519";expires=1700000060;'
                        'nonce="bm9uY2U=";tag="web-bot-auth"'),
}


class _TestSigner:
    """The seam's shape (public_key / sign / signature_agent) over a local test key."""

    def __init__(self, private=None, *, agent=AGENT):
        self.private = private or Ed25519PrivateKey.generate()
        self.signature_agent = agent
        self.fail = False

    def public_key(self):
        return self.private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def sign(self, message):
        if self.fail:
            raise RuntimeError("kms down")
        return self.private.sign(message)


def _verify(headers, authority, public_key):
    """Recompute the signature base a server builds from what it received, and verify."""
    params = re.fullmatch(r"sig1=(.*)", headers["Signature-Input"]).group(1)
    signature = base64.b64decode(re.fullmatch(r"sig1=:(.*):", headers["Signature"]).group(1))
    base = (f'"@authority": {authority}\n"signature-agent": {headers["Signature-Agent"]}\n'
            f'"@signature-params": {params}').encode("ascii")
    public_key.verify(signature, base)
    return params


# ── Signature shape ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("url, authority", [
    ("https://Example.COM/path?q=1", "example.com"),
    ("https://example.com:443/", "example.com"),
    ("http://example.com:80/", "example.com"),
    ("http://example.com:8443/", "example.com:8443"),
    ("https://[2001:DB8::1]:8443/", "[2001:db8::1]:8443"),
    ("http://127.0.0.1:9/", "127.0.0.1:9"),
])
def test_headers_verify_against_the_authority_the_server_sees(url, authority):
    signer = _TestSigner()
    headers = wba.RequestSigner(signer).headers(url)
    params = _verify(headers, authority, signer.private.public_key())
    assert headers["Signature-Agent"] == f'"{AGENT}"'
    assert '("@authority" "signature-agent")' in params and 'tag="web-bot-auth"' in params
    created, expires = (int(re.search(rf"{k}=(\d+)", params).group(1)) for k in ("created", "expires"))
    assert expires - created == 60
    assert len(base64.b64decode(re.search(r'nonce="([^"]+)"', params).group(1))) == 64


def test_wrong_authority_does_not_verify():
    # The negative control for the test above: the same headers fail against another authority.
    signer = _TestSigner()
    headers = wba.RequestSigner(signer).headers("https://example.com:8443/")
    from cryptography.exceptions import InvalidSignature

    with pytest.raises(InvalidSignature):
        _verify(headers, "example.com", signer.private.public_key())


def test_bytes_match_ab1_sign_request():
    """Same key, created and nonce as weave-cloud AB-1's ``sign_request`` → byte-identical headers.

    ``AB1_SIGN_REQUEST`` is that function's output for these inputs."""
    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    headers = wba.RequestSigner(_TestSigner(private)).headers(
        "https://example.com/", created=1_700_000_000, nonce="bm9uY2U=")
    assert headers == AB1_SIGN_REQUEST


def test_non_http_urls_and_non_ascii_hosts_are_refused():
    signer = wba.RequestSigner(_TestSigner())
    for url in ("data:text/html,x", "https://bücher.example/", "file:///etc/hosts"):
        with pytest.raises(ValueError):
            signer.headers(url)


def test_a_signer_whose_signature_does_not_verify_is_refused():
    signer = _TestSigner()
    signer.sign = lambda message: b"\0" * 64  # before construction: RequestSigner binds sign then
    with pytest.raises(Exception):
        wba.RequestSigner(signer).headers("https://example.com/")


# ── Seam: flag and plugin hook ───────────────────────────────────────────────


@pytest.fixture
def config(monkeypatch):
    cfg = {"browser": {}}
    monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: cfg)
    monkeypatch.setattr(wba, "_resolved", None)
    return cfg


@pytest.fixture
def hook(monkeypatch):
    answers = []
    monkeypatch.setattr("hermes_cli.plugins.has_hook", lambda name: name == "browser_request_signer")
    monkeypatch.setattr("hermes_cli.plugins.iter_hook_callbacks",
                        lambda name: tuple(answers) if name == "browser_request_signer" else ())
    return answers


def test_flag_off_by_default_resolves_no_signer(config, hook):
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["browser"]["web_bot_auth"] is False
    hook.append(lambda: _TestSigner())
    assert wba.request_signer() is None
    config["browser"]["web_bot_auth"] = "true"  # only the boolean true turns it on
    assert wba.request_signer() is None


def test_flag_on_resolves_the_plugin_signer_once(config, hook):
    config["browser"]["web_bot_auth"] = True
    calls = []
    hook.append(lambda: calls.append(1) or _TestSigner())
    first = wba.request_signer()
    assert isinstance(first, wba.RequestSigner)
    assert wba.request_signer() is first and calls == [1]
    config["browser"]["web_bot_auth"] = False  # turning the flag off stops new supervisors signing
    assert wba.request_signer() is None


def test_hook_is_registered():
    from hermes_cli.plugins import VALID_HOOKS

    assert "browser_request_signer" in VALID_HOOKS


def test_failing_or_missing_signer_logs_and_returns_none(config, hook, caplog):
    config["browser"]["web_bot_auth"] = True
    caplog.set_level(logging.WARNING, logger=wba.__name__)
    assert wba.request_signer() is None
    assert "no plugin answers browser_request_signer" in caplog.text

    def boom():
        raise RuntimeError("secret-bearer-token")

    hook.append(boom)
    assert wba.request_signer() is None
    assert "RuntimeError" in caplog.text and "secret-bearer-token" not in caplog.text


# ── E2E: real Chrome, real supervisor, verifying server ──────────────────────


def _chrome():
    return os.environ.get("HERMES_E2E_CHROME") or shutil.which("google-chrome") or shutil.which("chromium")


e2e = pytest.mark.skipif(os.environ.get("HERMES_E2E_BROWSER", "").strip() != "1" or not _chrome(),
                         reason="real-browser E2E: set HERMES_E2E_BROWSER=1 and provide Chrome")


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, public_key):
        self.public_key, self.seen = public_key, []
        super().__init__(("127.0.0.1", 0), _Handler)


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        authority = f"127.0.0.1:{self.server.server_address[1]}"
        try:
            _verify(self.headers, authority, self.server.public_key)
            verdict = "signed"
        except Exception:
            verdict = "unsigned"
        self.server.seen.append((self.path, verdict))
        if self.path == "/start":
            self.send_response(302)
            self.send_header("Location", "/page")
            self.end_headers()
            return
        body = b'<html><body><img src="/pixel.png">page</body></html>' if self.path == "/page" else b"x"
        self.send_response(200)
        self.send_header("Content-Type", "text/html" if self.path == "/page" else "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def chrome():
    profile = tempfile.mkdtemp(prefix="hermes-wba-test-")
    proc = subprocess.Popen([_chrome(), "--remote-debugging-port=0", f"--user-data-dir={profile}", "--headless=new",
                             "--no-first-run", "--no-default-browser-check", "--disable-gpu", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port_file = os.path.join(profile, "DevToolsActivePort")
    deadline = time.monotonic() + 15
    while not os.path.exists(port_file) and time.monotonic() < deadline:
        time.sleep(0.1)
    port = open(port_file).readline().strip()
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5) as r:
        ws_url = json.loads(r.read())["webSocketDebuggerUrl"]
    yield ws_url
    proc.kill()
    proc.wait(timeout=5)
    shutil.rmtree(profile, ignore_errors=True)


def _load(supervisor, url, paths, server, timeout=10.0):
    """Navigate and return each of ``paths``' verdicts (Chrome's own /favicon.ico is not asserted on)."""
    supervisor.evaluate_runtime(f"location.href = {json.dumps(url)}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not set(paths) <= {p for p, _ in server.seen}:
        time.sleep(0.1)
    return {p: v for p, v in server.seen if p in paths}


@e2e
@pytest.mark.integration
@pytest.mark.parametrize("flag", [True, False])
def test_every_request_arrives_signed_only_when_the_flag_is_on(chrome, config, hook, flag):
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    signer = _TestSigner()
    config["browser"]["web_bot_auth"] = flag
    hook.append(lambda: signer)
    server = _Server(signer.private.public_key())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        supervisor = SUPERVISOR_REGISTRY.get_or_start(task_id="wba-e2e", cdp_url=chrome)
        seen = _load(supervisor, f"http://127.0.0.1:{server.server_address[1]}/start",
                     ["/start", "/page", "/pixel.png"], server)
        expected = "signed" if flag else "unsigned"
        assert seen == {"/start": expected, "/page": expected, "/pixel.png": expected}
    finally:
        SUPERVISOR_REGISTRY.stop_all()
        server.shutdown()


@e2e
@pytest.mark.integration
def test_headers_chrome_refuses_let_the_request_through_unsigned(chrome, config, hook, caplog, monkeypatch):
    """Chrome rejects the whole continue on an invalid header value; the request must not stay paused."""
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    signer = _TestSigner()
    config["browser"]["web_bot_auth"] = True
    hook.append(lambda: signer)
    monkeypatch.setattr(wba.RequestSigner, "headers", lambda self, url: {"Signature-Agent": "bad\nvalue"})
    server = _Server(signer.private.public_key())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    caplog.set_level(logging.WARNING, logger="tools.browser_supervisor")
    try:
        supervisor = SUPERVISOR_REGISTRY.get_or_start(task_id="wba-e2e-refused", cdp_url=chrome)
        seen = _load(supervisor, f"http://127.0.0.1:{server.server_address[1]}/page", ["/page", "/pixel.png"], server)
        assert seen == {"/page": "unsigned", "/pixel.png": "unsigned"}
        assert "signed continue refused" in caplog.text
    finally:
        SUPERVISOR_REGISTRY.stop_all()
        server.shutdown()


@e2e
@pytest.mark.integration
def test_a_failing_signer_lets_requests_through_unsigned_and_logs(chrome, config, hook, caplog):
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    signer = _TestSigner()
    config["browser"]["web_bot_auth"] = True
    hook.append(lambda: signer)
    server = _Server(signer.private.public_key())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    caplog.set_level(logging.WARNING, logger="tools.browser_supervisor")
    try:
        supervisor = SUPERVISOR_REGISTRY.get_or_start(task_id="wba-e2e-fail", cdp_url=chrome)
        signer.fail = True
        seen = _load(supervisor, f"http://127.0.0.1:{server.server_address[1]}/page", ["/page", "/pixel.png"], server)
        assert seen == {"/page": "unsigned", "/pixel.png": "unsigned"}
        assert "signing failed (RuntimeError); request continues unsigned" in caplog.text
    finally:
        SUPERVISOR_REGISTRY.stop_all()
        server.shutdown()


@e2e
@pytest.mark.integration
@pytest.mark.skipif(not shutil.which("agent-browser"), reason="agent-browser not on PATH")
@pytest.mark.live_system_guard_bypass  # cleanup_browser stops the real agent-browser daemon it started
def test_a_local_agent_browser_session_signs_its_first_navigation(config, hook, monkeypatch):
    """The Sprite path: a local ``--session`` has no CDP URL, so the tool asks the daemon for one and attaches the
    signing supervisor before the navigation it was called for."""
    from tools import browser_tool

    signer = _TestSigner()
    config["browser"]["web_bot_auth"] = True
    hook.append(lambda: signer)
    monkeypatch.setenv("AGENT_BROWSER_EXECUTABLE_PATH", _chrome())
    monkeypatch.setattr(browser_tool, "_allow_private_urls", lambda: True)  # the verifying server is on loopback
    server = _Server(signer.private.public_key())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    task = "wba-local-e2e"
    try:
        out = json.loads(browser_tool.browser_navigate(f"http://127.0.0.1:{server.server_address[1]}/start",
                                                       task_id=task))
        assert out.get("success"), out
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and "/pixel.png" not in dict(server.seen):
            time.sleep(0.1)
        assert {p: v for p, v in server.seen if p != "/favicon.ico"} == {
            "/start": "signed", "/page": "signed", "/pixel.png": "signed"}
    finally:
        browser_tool.cleanup_browser(task)
        server.shutdown()
