"""Harso vault V5: ``WeaveLoginBackend`` against a fake weave-api, and the three fork seams.

The fake is a real HTTP server on 127.0.0.1 speaking the V3 route contract (weave-cloud
``services/weave-api/weave_api/vault.py``: runtime list/meta, ``POST /v1/vault/resolve``, error body
``{"error": {"code", "message"}}``). It is driven through the real backend, the real httpx client, the real
browser tools and a real temp ``state.db``; only the page (CDP eval) is faked.

Proves: the model-facing surface never carries a value (tool results, logs, raised errors); a fill names the
exact page origin and a mismatch is refused before any value moves; the once-card run identity is the admitted
native submit's ``native_request_ref`` in its lineage-root conversation; OTP codes come from the server and are
never minted locally; save-login goes through the backend and never prompts for a password the Harso vault must
not receive; and nothing changes for a profile that does not select the weave backend.
"""

from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest
import yaml

CONV = "0199aaaa-0000-7000-8000-000000000001"
ROTATED = "0199aaaa-0000-7000-8000-000000000002"
ITEM = "0199bbbb-0000-7000-8000-00000000000a"
ADDR = "0199bbbb-0000-7000-8000-00000000000b"
KEY = "0199bbbb-0000-7000-8000-00000000000c"
HANDLE, ADDR_HANDLE, KEY_HANDLE = f"wv:{ITEM}", f"wv:{ADDR}", f"wv:{KEY}"
CELL_BEARER = "wvc1_" + "C" * 43
ATTEMPT_BEARER = "wva1_" + "A" * 43
ORIGIN = "https://www.amazon.com"
PASSWORD = "canary-Pw-7f3e1d9c-never-in-output"
OTP = "482913"
COMMAND_ID = "0199cccc-0000-7000-8000-0000000000c1"
NATIVE_REF = "4f1c2b0e9d8a7c6b5a4f3e2d1c0b9a88"

_CONTROLS = [{"autocomplete": "username", "formIndex": 0, "index": 0, "label": "", "name": "email", "type": "email"},
             {"autocomplete": "current-password", "formIndex": 0, "index": 1, "label": "", "name": "pw",
              "type": "password"}]


def _item(item_id, kind, origins, **extra):
    return {"id": f"wv:{item_id}", "kind": kind, "label": kind.title(), "origin": origins[0],
            "created_at": "2026-09-25T00:00:00Z", "identifier_type": extra.get("identifier_type"),
            "identifier": extra.get("identifier"), "has_otp": extra.get("has_otp", False),
            "allowed_origins": origins}


class FakeWeaveApi:
    """The V3 runtime routes, with the authority's origin/grant decision reduced to a table."""

    def __init__(self):
        self.requests: list = []
        self.items = {
            ITEM: _item(ITEM, "login", [ORIGIN], identifier="jane@example.com", identifier_type="email", has_otp=True),
            ADDR: _item(ADDR, "address", ["https://shop.example"]),
            KEY: _item(KEY, "api_key", ["https://api.example"]),
        }
        self.decision = "once"          # once | always | approval_required
        self.force: tuple | None = None  # (status, body) override for every call
        self.force_resolve: tuple | None = None  # (status, body) override for POST /v1/vault/resolve only
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                api.requests.append({"method": method, "path": self.path,
                                     "authorization": self.headers.get("Authorization"), "body": body})
                if api.force is not None:
                    return self._send(*api.force)
                auth = self.headers.get("Authorization") or ""
                if auth not in (f"Bearer {CELL_BEARER}", f"Bearer {ATTEMPT_BEARER}"):
                    return self._send(401, {"error": {"code": "UNAUTHENTICATED", "message": "Bearer token is required"}})
                if method == "GET" and self.path == "/v1/vault/runtime/items":
                    return self._send(200, {"items": list(api.items.values())})
                if method == "GET" and self.path.startswith("/v1/vault/runtime/items/wv:"):
                    item = api.items.get(self.path.rsplit("wv:", 1)[1])
                    if item is None:
                        return self._send(404, {"error": {"code": "VAULT_ITEM_UNAVAILABLE", "message": "Vault item was not found"}})
                    return self._send(200, item)
                if method == "POST" and self.path == "/v1/vault/resolve":
                    if api.force_resolve is not None:
                        return self._send(*api.force_resolve)
                    item = api.items.get(body["handle"].removeprefix("wv:"))
                    if item is None:
                        return self._send(404, {"error": {"code": "VAULT_ITEM_UNAVAILABLE", "message": "Vault item was not found"}})
                    if body["page_origin"] not in item["allowed_origins"]:
                        return self._send(403, {"error": {"code": "VAULT_ORIGIN_REFUSED", "message": "This item is not bound to this site"}})
                    if api.decision == "approval_required":
                        return self._send(200, {"decision": "approval_required", "approval_ref": "0199dddd-0000-7000-8000-000000000001",
                                                "choices": ["once", "always", "deny"]})
                    value = {"fill_login": {"password": PASSWORD}, "enter_otp": {"otp": OTP},
                             "fill_address": {"fields": {"address_line1": "1 Canary Lane", "city": "Springfield",
                                                         "postal_code": "12345", "country": "US"}}}[body["action"]]
                    return self._send(200, {"decision": api.decision, "action": body["action"], **value})
                return self._send(404, {"error": {"code": "NOT_FOUND", "message": "no route"}})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def resolves(self):
        return [r for r in self.requests if r["path"] == "/v1/vault/resolve"]


@pytest.fixture
def api():
    fake = FakeWeaveApi()
    yield fake
    fake.server.shutdown()


def _write_config(cfg: dict) -> None:
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    from hermes_cli import config as config_mod

    for name in ("_invalidate_config_cache", "invalidate_config_cache", "clear_config_cache"):
        if callable(getattr(config_mod, name, None)):
            getattr(config_mod, name)()


@pytest.fixture
def weave(api, monkeypatch):
    """A Harso cell profile: vault on, weave backend, the cell's bearer in the process env."""
    _write_config({"vault": {"enabled": True, "backend": "weave", "weave_api_url": api.url}})
    monkeypatch.setenv("WEAVE_API_MCP_BEARER", CELL_BEARER)
    with patch("agent.vault_backends.base._cfg",
               return_value={"enabled": True, "backend": "weave", "weave_api_url": api.url}):
        yield api


@pytest.fixture
def admitted_turn():
    """A real state.db: conversation session, a rotation fork of it, and the native submit admitted into the
    fork. The current tool call runs under that submit's command id as its turn id (gateway/run.py staging)."""
    from hermes_state import SessionDB
    from tools.approval import reset_current_observability_context, set_current_observability_context

    db = SessionDB()
    db.create_session(f"weave-{CONV}", "api_server")
    db.create_session(f"weave-{ROTATED}", "api_server", parent_session_id=f"weave-{CONV}")
    db.register_native_session_submit(f"weave-{ROTATED}", external_request_id=COMMAND_ID,
                                      message_sha256="0" * 64, native_request_ref=NATIVE_REF)
    db.set_native_session_submit_admission(native_request_ref=NATIVE_REF, admission="streaming")
    db.close()
    tokens = set_current_observability_context(turn_id=COMMAND_ID, session_id=f"weave-{ROTATED}")
    yield
    reset_current_observability_context(tokens)


@contextmanager
def _page(origin=ORIGIN, controls=_CONTROLS):
    """Fake only the page: origin, input inspection, and the CDP secret injection (captured)."""
    from tools import browser_vault_tool

    injected: list = []

    def fill(task_id, expression):
        injected.append(expression)
        return {"success": True, "result": json.dumps({"filled": 1})}

    with patch.object(browser_vault_tool, "_current_page_origin", return_value=origin), \
         patch.object(browser_vault_tool, "_focus_bound_origin", return_value=None), \
         patch.object(browser_vault_tool, "_eval_js", return_value={"success": True, "result": json.dumps(controls)}), \
         patch.object(browser_vault_tool, "_eval_js_secret", side_effect=fill):
        yield injected


@pytest.fixture(autouse=True)
def _clear_redaction():
    yield
    from agent import redact

    redact.clear_vault_redaction_values()


# ---------------------------------------------------------------------------------------------------------------
# Backend selection: inert unless selected
# ---------------------------------------------------------------------------------------------------------------

def test_defaults_keep_the_vault_off_and_local():
    from agent.vault_backends.base import enabled_backends
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    from tools.browser_vault_tool import _vault_opted_in

    assert DEFAULT_CONFIG["vault"] == {"enabled": False, "backend": "local", "weave_api_url": ""}
    assert _vault_opted_in() is False
    assert [b.name for b in enabled_backends()] == ["local"]


def test_weave_backend_is_the_only_source_when_selected(api):
    from agent.vault_backends.base import backend_for_handle, enabled_backends

    with patch("agent.vault_backends.base._cfg", return_value={"backend": "weave", "weave_api_url": api.url}):
        assert [b.name for b in enabled_backends()] == ["weave"]
        assert backend_for_handle("vault_abc123") is None  # a local handle is not routable in a cell
        assert backend_for_handle(HANDLE).name == "weave"


def test_real_config_yaml_selects_weave(api):
    """The selection is read from the profile's real config.yaml, not only a patched section."""
    from agent.vault_backends.base import enabled_backends

    _write_config({"vault": {"enabled": True, "backend": "weave", "weave_api_url": api.url}})
    assert [b.name for b in enabled_backends()] == ["weave"]


@pytest.mark.parametrize("value", ["Weave", "1password", 1, None])
def test_an_unknown_backend_leaves_no_source_rather_than_falling_back(value):
    from agent.vault_backends.base import enabled_backends

    with patch("agent.vault_backends.base._cfg", return_value={"backend": value}):
        assert enabled_backends() == []


# ---------------------------------------------------------------------------------------------------------------
# Fake weave-api contract
# ---------------------------------------------------------------------------------------------------------------

def test_list_is_metadata_only_by_handle_and_hides_api_keys(weave):
    from tools.browser_vault_tool import browser_vault_list

    raw = browser_vault_list()
    out = json.loads(raw)
    assert [(i["handle"], i["kind"], i["backend"]) for i in out["items"]] == [
        (HANDLE, "login", "weave"), (ADDR_HANDLE, "address", "weave")]
    assert out["items"][0]["identifier"] == "jane@example.com" and out["items"][0]["two_factor"] == "automatic"
    assert KEY_HANDLE not in raw  # an api_key is injected by the egress proxy, never resolved by a runtime
    [call] = weave.requests
    assert (call["method"], call["path"], call["authorization"]) == ("GET", "/v1/vault/runtime/items", f"Bearer {CELL_BEARER}")


def test_meta_is_one_get_by_handle_and_a_malformed_handle_never_reaches_the_network(weave):
    from agent.vault_backends.weave import WeaveLoginBackend

    backend = WeaveLoginBackend(weave.url)
    meta = backend.get_meta(HANDLE)
    assert (meta.id, meta.origin, meta.allowed_origins, meta.has_otp) == (HANDLE, ORIGIN, (ORIGIN,), True)
    assert backend.get_meta(f"wv:{ITEM[:-1]}f") is None  # unknown -> 404 -> None
    assert backend.get_meta("wv:../../v1/vault/items") is None
    assert [r["path"] for r in weave.requests] == [f"/v1/vault/runtime/items/{HANDLE}",
                                                   f"/v1/vault/runtime/items/wv:{ITEM[:-1]}f"]


def test_fill_resolves_once_for_the_exact_origin_with_the_admitted_run(weave, admitted_turn):
    from tools.browser_vault_tool import browser_vault_fill

    with _page() as injected:
        raw = browser_vault_fill(HANDLE, task_id="t")
    out = json.loads(raw)
    assert (out["success"], out["filled_fields"], out["backend"], out["origin"]) == (True, 1, "weave", ORIGIN)
    [resolve] = weave.resolves()
    assert resolve["body"] == {"handle": HANDLE, "action": "fill_login", "page_origin": ORIGIN,
                               "conversation_id": CONV, "run_id": NATIVE_REF}
    assert resolve["authorization"] == f"Bearer {CELL_BEARER}"
    assert PASSWORD in injected[0]  # the value reached the page injection, and only there
    assert PASSWORD not in raw


def test_a_work_attempt_is_its_own_run_and_names_no_conversation(weave, monkeypatch):
    from tools.browser_vault_tool import browser_vault_fill

    monkeypatch.setenv("WEAVE_API_MCP_BEARER", ATTEMPT_BEARER)
    with _page():
        out = json.loads(browser_vault_fill(HANDLE, task_id="t"))
    assert out["success"] is True
    assert weave.resolves()[0]["body"] == {"handle": HANDLE, "action": "fill_login", "page_origin": ORIGIN}


def test_a_cell_turn_the_app_did_not_send_resolves_nothing(weave):
    """No admitted native submit for this turn: no run exists, so no once card could ever be spent by it."""
    from tools.approval import reset_current_observability_context, set_current_observability_context
    from tools.browser_vault_tool import browser_vault_fill

    tokens = set_current_observability_context(turn_id="weave-x:task:deadbeef")
    try:
        with _page() as injected:
            out = json.loads(browser_vault_fill(HANDLE, task_id="t"))
    finally:
        reset_current_observability_context(tokens)
    assert (out["success"], out["error_type"]) == (False, "no_run")
    assert weave.resolves() == [] and injected == []


def test_origin_mismatch_is_refused_before_any_resolve(weave, admitted_turn):
    """Scheme, subdomain and port tricks: the tool refuses on the item's own origins; weave-api is never asked."""
    from tools.browser_vault_tool import browser_vault_fill

    for page in ("http://www.amazon.com", "https://amazon.com", "https://www.amazon.com.evil.example",
                 "https://www.amazon.com:8443"):
        with _page(origin=page) as injected:
            out = json.loads(browser_vault_fill(HANDLE, task_id="t"))
        assert (out["success"], out["error_type"]) == (False, "origin_mismatch"), page
        assert injected == []
    assert weave.resolves() == []


def test_the_authority_refusing_the_origin_is_a_typed_refusal_with_nothing_filled(weave, admitted_turn):
    """Defence in depth: the item's metadata still lists the page origin, but weave-api (the authority, which
    re-reads the item) refuses it, e.g. the owner removed the site after the model listed it."""
    from tools.browser_vault_tool import browser_vault_fill

    weave.force_resolve = (403, {"error": {"code": "VAULT_ORIGIN_REFUSED", "message": "This item is not bound to this site"}})
    with _page() as injected:
        out = json.loads(browser_vault_fill(HANDLE, task_id="t"))
    assert (out["success"], out["error_type"]) == (False, "origin_mismatch")
    assert injected == [] and len(weave.resolves()) == 1


def test_approval_required_fills_nothing_and_tells_the_model_to_wait(weave, admitted_turn):
    from tools.browser_vault_tool import browser_vault_fill

    weave.decision = "approval_required"
    with _page() as injected:
        out = json.loads(browser_vault_fill(HANDLE, task_id="t"))
    assert (out["success"], out["error_type"]) == (False, "approval_required")
    assert "Harso app" in out["error"] and injected == []


@pytest.mark.parametrize("status,code,error_type", [(429, "VAULT_RATE_LIMITED", "rate_limited"),
                                                    (409, "VAULT_USE_STALE", "item_changed"),
                                                    (403, "VAULT_ACTION_REFUSED", "action_refused")])
def test_authority_refusals_map_to_typed_results(weave, admitted_turn, status, code, error_type):
    from tools.browser_vault_tool import browser_vault_fill

    weave.force_resolve = (status, {"error": {"code": code, "message": "refused"}})
    with _page() as injected:
        out = json.loads(browser_vault_fill(HANDLE, task_id="t"))
    assert (out["success"], out["error_type"]) == (False, error_type) and injected == []


def test_address_fill_resolves_fields_for_the_exact_origin(weave, admitted_turn):
    from tools.browser_vault_tool import browser_vault_fill

    controls = [{"autocomplete": "address-line1", "formIndex": 0, "index": 0, "label": "", "name": "a1", "type": "text"},
                {"autocomplete": "postal-code", "formIndex": 0, "index": 1, "label": "", "name": "zip", "type": "text"}]
    with _page(origin="https://shop.example", controls=controls) as injected:
        out = json.loads(browser_vault_fill(ADDR_HANDLE, task_id="t"))
    assert out["success"] is True and out["fields"] == ["address-line1", "postal-code"]
    assert weave.resolves()[0]["body"]["action"] == "fill_address"
    assert "1 Canary Lane" in injected[0]


# ---------------------------------------------------------------------------------------------------------------
# Server OTP
# ---------------------------------------------------------------------------------------------------------------

def test_otp_is_minted_by_the_server_never_locally(weave, admitted_turn):
    from tools.browser_vault_tool import browser_vault_enter_code

    controls = [{"index": 0, "type": "text", "name": "otp", "autocomplete": "one-time-code"}]
    with patch("agent.vault_store.totp_now", side_effect=AssertionError("no local TOTP in a cell")), \
         _page(controls=controls) as injected:
        raw = browser_vault_enter_code(HANDLE, task_id="t")
    out = json.loads(raw)
    assert (out["success"], out["source"], out["origin"]) == (True, "weave", ORIGIN)
    assert weave.resolves()[0]["body"] == {"handle": HANDLE, "action": "enter_otp", "page_origin": ORIGIN,
                                           "conversation_id": CONV, "run_id": NATIVE_REF}
    assert OTP in injected[0] and OTP not in raw


def test_otp_approval_pending_is_the_answer_the_user_is_not_asked_instead(weave, admitted_turn):
    from agent.vault_backends import unlock as unlock_mod
    from tools.browser_vault_tool import browser_vault_enter_code

    weave.decision = "approval_required"
    asked = []
    unlock_mod.set_code_prompt_callback(lambda site, hint: asked.append(site) or "000000")
    try:
        with patch("agent.vault_backends.unlock.can_prompt_here", return_value=True), \
             _page(controls=[{"index": 0, "type": "text", "name": "otp", "autocomplete": "one-time-code"}]) as injected:
            out = json.loads(browser_vault_enter_code(HANDLE, task_id="t"))
    finally:
        unlock_mod.set_code_prompt_callback(None)
    assert (out["success"], out["error_type"]) == (False, "approval_required")
    assert asked == [] and injected == []


def test_an_item_without_an_authenticator_key_is_never_resolved_for_a_code(weave, admitted_turn):
    from tools.browser_vault_tool import browser_vault_enter_code

    weave.items[ITEM]["has_otp"] = False
    with patch("agent.vault_backends.unlock.can_prompt_here", return_value=False), \
         _page(controls=[{"index": 0, "type": "text", "name": "otp", "autocomplete": "one-time-code"}]):
        out = json.loads(browser_vault_enter_code(HANDLE, task_id="t"))
    assert out["error_type"] == "prompt_unavailable" and weave.resolves() == []


# ---------------------------------------------------------------------------------------------------------------
# Save-login through the backend
# ---------------------------------------------------------------------------------------------------------------

def test_save_login_under_weave_never_prompts_for_a_password_and_stores_nothing(weave, admitted_turn):
    from agent.vault_backends import unlock as unlock_mod
    from agent.vault_store import VaultStore
    from hermes_constants import get_hermes_home
    from tools.browser_vault_tool import browser_vault_save_login

    asked = []
    unlock_mod.set_save_login_prompt_callback(lambda origin, site: asked.append(origin) or
                                              {"identifier": "jane@example.com", "password": PASSWORD})
    try:
        with patch("agent.vault_backends.unlock.can_prompt_here", return_value=True), _page() as injected:
            raw = browser_vault_save_login(task_id="t")
    finally:
        unlock_mod.set_save_login_prompt_callback(None)
    out = json.loads(raw)
    assert (out["success"], out["error_type"]) == (False, "save_in_app")
    assert "Settings → Vault" in out["error"] and PASSWORD not in raw
    assert asked == [] and injected == [] and weave.requests == []
    assert not (get_hermes_home() / "vault").exists()
    assert VaultStore().list_items() == []


def test_save_login_under_local_goes_through_the_local_backend(tmp_path):
    from agent.vault_backends import unlock as unlock_mod
    from agent.vault_backends.local import LocalLoginBackend
    from agent.vault_store import VaultStore
    from tools import browser_vault_tool

    store = VaultStore(base_dir=tmp_path / "vault")
    saved = []
    real = LocalLoginBackend.save_login

    def spy(self, *args):
        saved.append(args[:4])
        return real(self, *args)

    unlock_mod.set_save_login_prompt_callback(lambda origin, site: {"identifier": "jane", "password": PASSWORD})
    try:
        with patch("agent.vault_store.get_vault_store", return_value=store), \
             patch.object(LocalLoginBackend, "save_login", spy), \
             patch("agent.vault_backends.unlock.can_prompt_here", return_value=True), \
             patch.object(browser_vault_tool, "browser_vault_fill", lambda h, task_id=None: json.dumps({"success": True})), \
             patch.object(browser_vault_tool, "_current_page_origin", return_value="https://acme.test"), \
             patch.object(browser_vault_tool, "_focus_bound_origin", return_value=None):
            out = json.loads(browser_vault_tool.browser_vault_save_login(task_id="t"))
    finally:
        unlock_mod.set_save_login_prompt_callback(None)
    assert out["success"] is True and saved == [("acme.test", "https://acme.test", "jane", "username")]
    assert store.resolve_secret(out["handle"]) == {"password": PASSWORD}


# ---------------------------------------------------------------------------------------------------------------
# Redaction: no value in tool output, logs, or raised errors
# ---------------------------------------------------------------------------------------------------------------

def test_no_value_reaches_tool_output_or_logs_on_success_or_failure(weave, admitted_turn, caplog):
    from agent.redact import redact_registered_vault_values
    from tools.browser_vault_tool import browser_vault_enter_code, browser_vault_fill, browser_vault_list

    caplog.set_level(logging.DEBUG)
    outputs = [browser_vault_list()]
    with _page():
        outputs.append(browser_vault_fill(HANDLE, task_id="t"))
    # The page echoes the injected password in its error: the tool must scrub it.
    from tools import browser_vault_tool

    with _page(), patch.object(browser_vault_tool, "_eval_js_secret",
                               return_value={"success": False, "error": f"Uncaught: {PASSWORD}"}):
        outputs.append(browser_vault_fill(HANDLE, task_id="t"))
    with _page(controls=[{"index": 0, "type": "text", "name": "otp", "autocomplete": "one-time-code"}]):
        outputs.append(browser_vault_enter_code(HANDLE, task_id="t"))
    # A server that answers 500 echoing a value and the bearer: neither may surface.
    weave.force = (500, {"error": {"code": "BOOM", "message": f"{PASSWORD} {CELL_BEARER}"}})
    with _page():
        outputs.append(browser_vault_fill(HANDLE, task_id="t"))
    weave.force = None

    blob = "\n".join(outputs) + "\n" + caplog.text
    for value in (PASSWORD, OTP, CELL_BEARER, "1 Canary Lane"):
        assert value not in blob, value
    # Registered with the model-egress boundary, so a later browser result echoing it is scrubbed.
    assert PASSWORD not in redact_registered_vault_values(f"page says {PASSWORD}")
    assert OTP not in redact_registered_vault_values(f"code {OTP}")


def test_backend_errors_are_content_free(api, monkeypatch):
    from agent.vault_backends.base import VaultUnavailable
    from agent.vault_backends.weave import WeaveLoginBackend

    monkeypatch.setenv("WEAVE_API_MCP_BEARER", ATTEMPT_BEARER)
    api.force = (500, {"error": {"code": "BOOM", "message": PASSWORD}})
    with pytest.raises(VaultUnavailable) as caught:
        WeaveLoginBackend(api.url).resolve_password(HANDLE, origin=ORIGIN)
    assert str(caught.value) == "the Harso vault answered HTTP 500"
    assert caught.value.__cause__ is None or PASSWORD not in str(caught.value.__cause__)

    with pytest.raises(VaultUnavailable) as unreachable:
        WeaveLoginBackend("http://127.0.0.1:9").list_items()
    assert ATTEMPT_BEARER not in str(unreachable.value) and unreachable.value.__cause__ is None

    monkeypatch.delenv("WEAVE_API_MCP_BEARER")
    with pytest.raises(VaultUnavailable, match="not configured"):
        WeaveLoginBackend(api.url).list_items()
    with pytest.raises(VaultUnavailable, match="not configured"):
        WeaveLoginBackend("").list_items()


def test_a_resolve_answer_for_another_action_or_without_a_decision_is_rejected(api, monkeypatch):
    from agent.vault_backends.base import VaultUnavailable
    from agent.vault_backends.weave import WeaveLoginBackend

    monkeypatch.setenv("WEAVE_API_MCP_BEARER", ATTEMPT_BEARER)
    for answer in ({"decision": "once", "action": "enter_otp", "password": PASSWORD},
                   {"decision": "deny", "action": "fill_login", "password": PASSWORD},
                   {"action": "fill_login", "password": PASSWORD},
                   {"decision": "once", "action": "fill_login", "password": ""}):
        api.force = (200, answer)
        with pytest.raises(VaultUnavailable) as caught:
            WeaveLoginBackend(api.url).resolve_password(HANDLE, origin=ORIGIN)
        assert PASSWORD not in str(caught.value)


def test_resolve_without_an_origin_is_refused_before_any_request(api, monkeypatch):
    from agent.vault_backends.base import VaultUseRefused
    from agent.vault_backends.weave import WeaveLoginBackend

    monkeypatch.setenv("WEAVE_API_MCP_BEARER", ATTEMPT_BEARER)
    for call in (lambda b: b.resolve_password(HANDLE), lambda b: b.resolve_otp(HANDLE, origin=""),
                 lambda b: b.resolve_secret(ADDR_HANDLE)):
        with pytest.raises(VaultUseRefused) as caught:
            call(WeaveLoginBackend(api.url))
        assert caught.value.error_type == "origin_required"
    assert api.requests == []


def test_native_submit_run_needs_an_admitted_submit(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        assert db.native_submit_run(COMMAND_ID) is None  # table absent
        db.create_session(f"weave-{CONV}", "api_server")
        db.register_native_session_submit(f"weave-{CONV}", external_request_id=COMMAND_ID,
                                          message_sha256="0" * 64, native_request_ref=NATIVE_REF)
        assert db.native_submit_run(COMMAND_ID) is None  # reserved, not admitted
        db.set_native_session_submit_admission(native_request_ref=NATIVE_REF, admission="queued")
        assert db.native_submit_run(COMMAND_ID) == {"session_id": f"weave-{CONV}", "native_request_ref": NATIVE_REF,
                                                    "root_session_id": f"weave-{CONV}"}
        assert db.native_submit_run("") is None
    finally:
        db.close()
