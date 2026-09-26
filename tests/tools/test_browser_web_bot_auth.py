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
        self.hang: "threading.Event | None" = None  # sign() blocks until it is set

    def public_key(self):
        return self.private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def sign(self, message):
        if self.hang is not None:
            self.hang.wait(10)
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


# ── Seam: flag, plugin hook, deadlines ───────────────────────────────────────


@pytest.fixture
def config(monkeypatch):
    cfg = {"browser": {}}
    monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: cfg)
    monkeypatch.setattr(wba, "_resolved", None)
    monkeypatch.setattr(wba, "_resolving", None, raising=False)
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
    config["browser"]["web_bot_auth"] = False
    assert wba.request_signer() is None


def test_hook_is_registered():
    from hermes_cli.plugins import VALID_HOOKS

    assert "browser_request_signer" in VALID_HOOKS


def test_failing_or_missing_signer_logs_returns_none_and_recovers(config, hook, caplog):
    config["browser"]["web_bot_auth"] = True
    caplog.set_level(logging.WARNING, logger=wba.__name__)
    assert wba.request_signer() is None
    assert "no plugin answers browser_request_signer" in caplog.text

    def boom():
        raise RuntimeError("secret-bearer-token")

    hook.append(boom)
    assert wba.request_signer() is None
    assert "RuntimeError" in caplog.text and "secret-bearer-token" not in caplog.text
    hook[:] = [lambda: _TestSigner()]  # the plugin comes back: the next call resolves it
    assert isinstance(wba.request_signer(), wba.RequestSigner)


@pytest.mark.parametrize("stage", ["hook", "public_key"])
def test_a_hung_signer_resolution_is_bounded_single_flight_and_recovers(config, hook, monkeypatch, caplog, stage):
    """A plugin (or its public_key call to weave-api/KMS) that never answers delays browsing by at most the
    deadline, once: later callers do not wait again, and no second resolution starts while one hangs."""
    monkeypatch.setattr(wba, "SIGNER_TIMEOUT_S", 0.3, raising=False)
    config["browser"]["web_bot_auth"] = True
    release, entered = threading.Event(), []

    class Hung(_TestSigner):
        def public_key(self):
            if stage == "public_key":
                entered.append(1)
                release.wait(10)
            return super().public_key()

    def answer():
        if stage == "hook":
            entered.append(1)
            release.wait(10)
        return Hung()

    hook.append(answer)
    caplog.set_level(logging.WARNING, logger=wba.__name__)
    started = time.monotonic()
    assert wba.request_signer() is None
    assert time.monotonic() - started < 1.0
    assert "not ready within" in caplog.text
    started = time.monotonic()
    for _ in range(5):
        assert wba.request_signer() is None
    assert time.monotonic() - started < 0.2 and entered == [1]
    release.set()
    deadline = time.monotonic() + 5
    while wba._resolving is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert isinstance(wba.request_signer(), wba.RequestSigner)


def test_a_hung_sign_times_out_without_piling_up_threads(monkeypatch):
    monkeypatch.setattr(wba, "SIGNER_TIMEOUT_S", 0.2, raising=False)
    monkeypatch.setattr(wba, "_slots", threading.BoundedSemaphore(2), raising=False)
    release = threading.Event()
    signer = _TestSigner()
    signer.hang = release
    request_signer = wba.RequestSigner(signer)
    before = threading.active_count()
    for _ in range(6):
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            request_signer.headers("https://example.com/")
        assert time.monotonic() - started < 0.5
    assert threading.active_count() - before <= 2  # hung calls keep their slot; nothing new starts
    release.set()
    deadline = time.monotonic() + 5
    while threading.active_count() > before and time.monotonic() < deadline:
        time.sleep(0.02)
    _verify(request_signer.headers("https://example.com/"), "example.com", signer.private.public_key())


# ── Request classification: only the dialog bridge is exempt ─────────────────


@pytest.mark.parametrize("url, bridge", [
    ("http://hermes-dialog-bridge.invalid/?kind=alert&message=x", True),
    ("http://hermes-dialog-bridge.invalid/", True),
    ("http://127.0.0.1:9/hermes-dialog-bridge.invalid/not-a-bridge", False),
    ("http://127.0.0.1:9/ordinary?next=hermes-dialog-bridge.invalid", False),
    ("http://127.0.0.1:9/#hermes-dialog-bridge.invalid", False),
    ("http://hermes-dialog-bridge.invalid.example/", False),
    ("http://evil-hermes-dialog-bridge.invalid/", False),
    ("http://hermes-dialog-bridge.invalid@127.0.0.1:9/", False),
    ("http://hermes-dialog-bridge.invalid:8080/", False),
    ("https://hermes-dialog-bridge.invalid/", False),
    ("http://[::1/", False),
])
def test_only_the_dialog_bridge_origin_is_exempt(url, bridge):
    from tools.browser_supervisor import _is_dialog_bridge

    assert _is_dialog_bridge(url) is bridge


# ── Live sessions follow the flag ────────────────────────────────────────────


def test_a_reused_supervisor_and_vault_reuse_take_the_current_flag(config, hook, monkeypatch):
    from tools import browser_supervisor as bs
    from tools import browser_vault_tool

    applied = []

    class Live:
        cdp_url = "ws://x"
        _thread = threading.current_thread()
        _loop = type("L", (), {"is_running": lambda self: True})()

        def set_request_signer(self, signer):
            applied.append(signer)

    registry = bs._SupervisorRegistry()
    registry._by_task["t"] = live = Live()
    monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)
    hook.append(lambda: _TestSigner())
    config["browser"]["web_bot_auth"] = True
    assert registry.get_or_start("t", "ws://x") is live
    config["browser"]["web_bot_auth"] = False
    assert registry.get_or_start("t", "ws://x") is live
    config["browser"]["web_bot_auth"] = True
    assert browser_vault_tool._ensure_supervisor("t") is live
    assert [type(s) for s in applied] == [wba.RequestSigner, type(None), wba.RequestSigner]


# ── E2E: real Chrome, real supervisor, verifying server ──────────────────────


def _chrome():
    return os.environ.get("HERMES_E2E_CHROME") or shutil.which("google-chrome") or shutil.which("chromium")


e2e = pytest.mark.skipif(os.environ.get("HERMES_E2E_BROWSER", "").strip() != "1" or not _chrome(),
                         reason="real-browser E2E: set HERMES_E2E_BROWSER=1 and provide Chrome")
LOOKALIKE = "hermes-dialog-bridge.invalid.example"  # Chrome maps it to 127.0.0.1 (see the chrome fixture)


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, public_key, tls=False):
        self.public_key, self.seen = public_key, []
        super().__init__(("127.0.0.1", 0), _Handler)
        self.scheme = "https" if tls else "http"
        if tls:
            self.socket = _tls_context().wrap_socket(self.socket, server_side=True)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def url(self, path, host="127.0.0.1"):
        return f"{self.scheme}://{host}:{self.server_address[1]}{path}"


def _tls_context():
    import datetime
    import ipaddress
    import ssl

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False).sign(key, hashes.SHA256()))
    folder = tempfile.mkdtemp(prefix="hermes-wba-tls-")
    cert_path, key_path = os.path.join(folder, "cert.pem"), os.path.join(folder, "key.pem")
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    shutil.rmtree(folder, ignore_errors=True)
    return context


def _verdict(headers, public_key):
    """From the raw headers the server received: ``absent`` (none of the three), ``signed`` (verifies against the
    authority this server sees, from Host) or ``invalid`` (anything else, including a partial set)."""
    if not any(headers.get(name) for name in wba.SIGNED_HEADERS):
        return "absent"
    try:
        _verify(headers, headers["Host"], public_key)
        return "signed"
    except Exception:
        return "invalid"


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.seen.append((self.path, _verdict(self.headers, self.server.public_key)))
        if self.path.startswith("/start"):
            self.send_response(302)
            self.send_header("Location", "/page")
            self.end_headers()
            return
        body = b"x"
        if self.path == "/page":
            # Subresources, two of which carry the bridge host in their path and query (F1).
            body = (b'<html><body><img src="/pixel.png"><img src="/hermes-dialog-bridge.invalid/not-a-bridge">'
                    b'<img src="/ordinary?next=hermes-dialog-bridge.invalid">page</body></html>')
        self.send_response(200)
        self.send_header("Content-Type", "text/html" if self.path == "/page" else "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


PAGE_PATHS = ["/start", "/page", "/pixel.png", "/hermes-dialog-bridge.invalid/not-a-bridge",
              "/ordinary?next=hermes-dialog-bridge.invalid"]


@pytest.fixture
def chrome():
    profile = tempfile.mkdtemp(prefix="hermes-wba-test-")
    proc = subprocess.Popen([_chrome(), "--remote-debugging-port=0", f"--user-data-dir={profile}", "--headless=new",
                             "--no-first-run", "--no-default-browser-check", "--disable-gpu",
                             "--ignore-certificate-errors", f"--host-resolver-rules=MAP {LOOKALIKE} 127.0.0.1",
                             "about:blank"],
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


@pytest.fixture
def root_pauses(monkeypatch):
    """Every browser-level ``Fetch.requestPaused`` the supervisor receives: the WBA interceptor at work."""
    from tools.browser_supervisor import CDPSupervisor

    seen = []
    original = CDPSupervisor._on_event

    async def spy(self, method, params, session_id):
        if method == "Fetch.requestPaused" and session_id is None:
            seen.append(params.get("request", {}).get("url"))
        return await original(self, method, params, session_id)

    monkeypatch.setattr(CDPSupervisor, "_on_event", spy)
    return seen


@pytest.fixture
def registry():
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    yield SUPERVISOR_REGISTRY
    SUPERVISOR_REGISTRY.stop_all()


def _load(supervisor, url, paths, server, timeout=10.0):
    """Navigate and return each of ``paths``' verdicts (Chrome's own /favicon.ico is not asserted on)."""
    server.seen.clear()
    supervisor.evaluate_runtime(f"location.href = {json.dumps(url)}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not set(paths) <= {p for p, _ in server.seen}:
        time.sleep(0.1)
    return {p: v for p, v in server.seen if p in paths}


def _dialog_round_trip(supervisor, server):
    """The genuine dialog bridge still works: an alert surfaces and is answered. On an https page the bridge's
    http XHR is mixed content Chrome blocks, with or without this feature, so there is nothing to check there."""
    if server.scheme == "https":
        return True
    supervisor.evaluate_runtime("setTimeout(() => alert('WBA-BRIDGE'), 50)")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not supervisor.snapshot().pending_dialogs:
        time.sleep(0.1)
    dialogs = supervisor.snapshot().pending_dialogs
    return bool(dialogs) and dialogs[0].message == "WBA-BRIDGE" and supervisor.respond_to_dialog("accept")["ok"]


@e2e
@pytest.mark.integration
@pytest.mark.parametrize("tls", [False, True], ids=["http", "https"])
def test_the_flag_governs_a_live_session_in_both_directions(chrome, config, hook, registry, root_pauses, tls):
    signer = _TestSigner()
    server = _Server(signer.private.public_key(), tls=tls)
    try:
        # Fresh OFF: no interception, no headers, the bridge works.
        supervisor = registry.get_or_start(task_id="wba-e2e", cdp_url=chrome)
        assert _load(supervisor, server.url("/start"), PAGE_PATHS, server) == dict.fromkeys(PAGE_PATHS, "absent")
        assert root_pauses == [] and _dialog_round_trip(supervisor, server)
        # OFF -> ON while no plugin answers yet: still unsigned; then the plugin appears and the same session signs.
        config["browser"]["web_bot_auth"] = True
        assert registry.get_or_start(task_id="wba-e2e", cdp_url=chrome) is supervisor
        assert _load(supervisor, server.url("/start"), PAGE_PATHS, server) == dict.fromkeys(PAGE_PATHS, "absent")
        hook.append(lambda: signer)
        assert registry.get_or_start(task_id="wba-e2e", cdp_url=chrome) is supervisor
        assert _load(supervisor, server.url("/start"), PAGE_PATHS, server) == dict.fromkeys(PAGE_PATHS, "signed")
        # A lookalike host is signed too; the bridge still works with interception on.
        seen = _load(supervisor, server.url("/lookalike", LOOKALIKE), ["/lookalike"], server)
        assert seen == {"/lookalike": "signed"} and _dialog_round_trip(supervisor, server)
        # ON -> OFF on the same session: headers and interception stop, the bridge keeps working.
        config["browser"]["web_bot_auth"] = False
        assert registry.get_or_start(task_id="wba-e2e", cdp_url=chrome) is supervisor
        root_pauses.clear()
        assert _load(supervisor, server.url("/start"), PAGE_PATHS, server) == dict.fromkeys(PAGE_PATHS, "absent")
        assert root_pauses == [] and _dialog_round_trip(supervisor, server)
    finally:
        server.shutdown()


@e2e
@pytest.mark.integration
def test_headers_chrome_refuses_let_the_request_through_unsigned(chrome, config, hook, registry, caplog, monkeypatch):
    """Chrome rejects the whole continue on an invalid header value; the request must not stay paused."""
    signer = _TestSigner()
    config["browser"]["web_bot_auth"] = True
    hook.append(lambda: signer)
    monkeypatch.setattr(wba.RequestSigner, "headers", lambda self, url: {"Signature-Agent": "bad\nvalue"})
    server = _Server(signer.private.public_key())
    caplog.set_level(logging.WARNING, logger="tools.browser_supervisor")
    try:
        supervisor = registry.get_or_start(task_id="wba-e2e-refused", cdp_url=chrome)
        seen = _load(supervisor, server.url("/page"), ["/page", "/pixel.png"], server)
        assert seen == {"/page": "absent", "/pixel.png": "absent"}
        assert "signed continue refused" in caplog.text
    finally:
        server.shutdown()


@e2e
@pytest.mark.integration
@pytest.mark.parametrize("failure", ["raises", "hangs"])
def test_a_failing_signer_lets_requests_through_unsigned_and_logs(chrome, config, hook, registry, caplog,
                                                                 monkeypatch, failure):
    monkeypatch.setattr(wba, "SIGNER_TIMEOUT_S", 0.5, raising=False)
    signer, release = _TestSigner(), threading.Event()
    config["browser"]["web_bot_auth"] = True
    hook.append(lambda: signer)
    server = _Server(signer.private.public_key())
    caplog.set_level(logging.WARNING, logger="tools.browser_supervisor")
    try:
        supervisor = registry.get_or_start(task_id="wba-e2e-fail", cdp_url=chrome)
        if failure == "raises":
            signer.fail = True
        else:
            signer.hang = release
        seen = _load(supervisor, server.url("/page"), ["/page", "/pixel.png"], server)
        assert seen == {"/page": "absent", "/pixel.png": "absent"}
        name = "RuntimeError" if failure == "raises" else "TimeoutError"
        assert f"signing failed ({name}); request continues unsigned" in caplog.text
    finally:
        release.set()
        server.shutdown()


@e2e
@pytest.mark.integration
@pytest.mark.skipif(not shutil.which("agent-browser"), reason="agent-browser not on PATH")
@pytest.mark.live_system_guard_bypass  # cleanup_browser stops the real agent-browser daemon it started
def test_a_local_agent_browser_session_signs_its_first_navigation_and_follows_the_flag(config, hook, monkeypatch):
    """The Sprite path: a local ``--session`` has no CDP URL, so the tool asks the daemon for one and attaches the
    signing supervisor before the navigation it was called for; turning the flag off stops the next command's."""
    from tools import browser_tool

    signer = _TestSigner()
    config["browser"]["web_bot_auth"] = True
    hook.append(lambda: signer)
    monkeypatch.setenv("AGENT_BROWSER_EXECUTABLE_PATH", _chrome())
    monkeypatch.setattr(browser_tool, "_allow_private_urls", lambda: True)  # the verifying server is on loopback
    server = _Server(signer.private.public_key())
    task = "wba-local-e2e"

    def navigate(path):
        server.seen.clear()
        out = json.loads(browser_tool.browser_navigate(server.url(path), task_id=task))
        assert out.get("success"), out
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and "/pixel.png" not in dict(server.seen):
            time.sleep(0.1)
        return {p: v for p, v in server.seen if p in ("/start", "/start?again", "/page", "/pixel.png")}

    try:
        assert navigate("/start") == {"/start": "signed", "/page": "signed", "/pixel.png": "signed"}
        config["browser"]["web_bot_auth"] = False
        assert navigate("/start?again") == {"/start?again": "absent", "/page": "absent", "/pixel.png": "absent"}
    finally:
        browser_tool.cleanup_browser(task)
        server.shutdown()
