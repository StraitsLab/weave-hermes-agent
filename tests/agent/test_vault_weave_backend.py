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

import hashlib
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
CARD = "0199dddd-0000-7000-8000-000000000001"

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
        self.decisions: list = []       # per-resolve decisions, consumed in order before ``decision``
        self.approval_ref = CARD
        self.password = PASSWORD
        self.address = {"address_line1": "1 Canary Lane", "city": "Springfield", "postal_code": "12345", "country": "US"}
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
                    if body["action"] == "inject":  # V-7b-1: authorize only; a new once/deny card, never a key
                        return self._send(200, {"decision": "approval_required", "approval_ref": api.approval_ref,
                                                "choices": ["once", "deny"]})
                    if body["page_origin"] not in item["allowed_origins"]:
                        return self._send(403, {"error": {"code": "VAULT_ORIGIN_REFUSED", "message": "This item is not bound to this site"}})
                    decision = api.decisions.pop(0) if api.decisions else api.decision
                    if decision == "approval_required":
                        return self._send(200, {"decision": "approval_required", "approval_ref": api.approval_ref,
                                                "choices": ["once", "always", "deny"]})
                    value = {"fill_login": {"password": api.password}, "enter_otp": {"otp": OTP},
                             "fill_address": {"fields": dict(api.address)}}[body["action"]]
                    return self._send(200, {"decision": decision, "action": body["action"], **value})
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

    assert DEFAULT_CONFIG["vault"] == {"enabled": False, "backend": "local", "weave_api_url": "",
                                       "weave_timeout_seconds": 10.0}
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


@pytest.mark.parametrize("settled", ["once", "always"])
def test_chat_approval_allows_fill_in_the_same_turn(weave, admitted_turn, settled):
    from tools.browser_vault_tool import browser_vault_fill

    weave.decisions = ["approval_required"]
    weave.decision = settled
    with _page() as injected, _acp_gate("once") as asked:
        raw = browser_vault_fill(HANDLE, task_id="t")
    assert json.loads(raw)["success"] is True and PASSWORD in injected[0]
    assert PASSWORD not in raw and asked == [f"plugin_rule:{CARD}"]
    first, second = weave.resolves()
    assert first["body"] == second["body"] == {
        "handle": HANDLE, "action": "fill_login", "page_origin": ORIGIN,
        "conversation_id": CONV, "run_id": NATIVE_REF,
    }
    assert first["authorization"] == second["authorization"] == f"Bearer {CELL_BEARER}"


@pytest.mark.parametrize("choice", ["deny", "timeout"])
def test_chat_approval_denial_never_fills_or_retries(weave, admitted_turn, choice):
    from tools.browser_vault_tool import browser_vault_fill

    weave.decision = "approval_required"
    with _page() as injected, _acp_gate(choice) as asked:
        out = json.loads(browser_vault_fill(HANDLE, task_id="t"))
    assert (out["success"], out["error_type"]) == (False, "denied")
    assert asked == [f"plugin_rule:{CARD}"] and injected == [] and len(weave.resolves()) == 1


def test_chat_approval_gate_exception_is_unavailable(weave, admitted_turn):
    from agent.vault_backends.base import VaultUnavailable
    from agent.vault_backends.weave import WeaveLoginBackend

    weave.decision = "approval_required"
    with patch("tools.approval.request_tool_approval", side_effect=RuntimeError("private gate error")) as gate:
        with pytest.raises(VaultUnavailable, match="approval request.*could not be raised") as error:
            WeaveLoginBackend(weave.url).resolve_password(HANDLE, origin=ORIGIN)
    assert "private gate error" not in str(error.value)
    assert gate.call_args.kwargs == {"rule_key": CARD} and len(weave.resolves()) == 1


def test_chat_approval_allow_without_ledger_decision_fails_closed(weave, admitted_turn):
    from tools.browser_vault_tool import browser_vault_fill

    weave.decision = "approval_required"
    with _page() as injected, _acp_gate("once") as asked:
        out = json.loads(browser_vault_fill(HANDLE, task_id="t"))
    assert (out["success"], out["error_type"]) == (False, "vault_unavailable")
    assert asked == [f"plugin_rule:{CARD}"] and injected == [] and len(weave.resolves()) == 2


# ---------------------------------------------------------------------------------------------------------------
# Work attempt: the vault card is decided through the attempt's own approval gate (V-4b)
# ---------------------------------------------------------------------------------------------------------------

@contextmanager
def _acp_gate(choice):
    """The ACP session's approval wiring (acp_adapter/server.py): interactive context plus the per-thread callback,
    which is what turns the gate into a permission request the attempt host raises as an attention."""
    from tools import approval, terminal_tool

    asked: list = []

    def callback(command, description, **kwargs):
        asked.append(kwargs.get("pattern_key"))
        return choice

    previous = terminal_tool._get_approval_callback()
    token = approval.set_hermes_interactive_context(True)
    terminal_tool.set_approval_callback(callback)
    try:
        yield asked
    finally:
        terminal_tool.set_approval_callback(previous)
        approval.reset_hermes_interactive_context(token)


@pytest.fixture
def work(weave, monkeypatch):
    """A Work attempt: its own bearer, and the Work home's ``approvals.mode: off`` (WEV-1532) in the real config."""
    monkeypatch.setenv("WEAVE_API_MCP_BEARER", ATTEMPT_BEARER)
    _write_config({"vault": {"enabled": True, "backend": "weave", "weave_api_url": weave.url},
                   "approvals": {"mode": "off"}})
    weave.decisions = ["approval_required"]
    return weave


@pytest.mark.parametrize("settled", ["once", "always"])
def test_a_work_attempt_asks_its_gate_by_card_ref_then_resolves_exactly_once_more(work, settled):
    from tools.browser_vault_tool import browser_vault_fill

    work.decision = settled
    with _page() as injected, _acp_gate("once") as asked:
        out = json.loads(browser_vault_fill(HANDLE, task_id="t"))
    assert out["success"] is True and PASSWORD in injected[0]
    assert asked == [f"plugin_rule:{CARD}"]
    first, second = work.resolves()
    assert first["body"] == second["body"] == {"handle": HANDLE, "action": "fill_login", "page_origin": ORIGIN}


def test_the_work_gate_fires_even_though_work_runs_with_approvals_off(work):
    """approvals.mode: off bypasses dangerous-command prompts in Work; it must not bypass a vault card."""
    from tools import approval
    from tools.browser_vault_tool import browser_vault_enter_code

    assert approval._get_approval_mode() == "off"
    controls = [{"index": 0, "type": "text", "name": "otp", "autocomplete": "one-time-code"}]
    with _page(controls=controls) as injected, _acp_gate("once") as asked:
        out = json.loads(browser_vault_enter_code(HANDLE, task_id="t"))
    assert out["success"] is True and OTP in injected[0]
    assert asked == [f"plugin_rule:{CARD}"] and [r["body"]["action"] for r in work.resolves()] == ["enter_otp"] * 2


@pytest.mark.parametrize("choice", ["deny", "timeout"])
def test_a_work_denial_fills_nothing_and_never_resolves_again(work, choice):
    from tools.browser_vault_tool import browser_vault_fill

    with _page() as injected, _acp_gate(choice) as asked:
        out = json.loads(browser_vault_fill(HANDLE, task_id="t"))
    assert (out["success"], out["error_type"]) == (False, "denied") and "Do not retry" in out["error"]
    assert asked == [f"plugin_rule:{CARD}"] and injected == [] and len(work.resolves()) == 1


def test_a_card_still_undecided_after_an_allow_fills_nothing(work):
    from tools.browser_vault_tool import browser_vault_fill

    work.decisions = ["approval_required", "approval_required"]
    with _page() as injected, _acp_gate("once") as asked:
        out = json.loads(browser_vault_fill(HANDLE, task_id="t"))
    assert (out["success"], out["error_type"]) == (False, "vault_unavailable")
    assert len(asked) == 1 and injected == [] and len(work.resolves()) == 2  # no third call, no loop


@pytest.mark.parametrize("ref", [None, "", "not-a-ref", "0199dddd-0000-4000-8000-000000000001",
                                 CARD.upper(), f"{CARD}\n", f"{CARD}:x", f"x{CARD}", 7])
def test_a_card_ref_that_is_not_an_exact_uuid7_never_reaches_the_gate(work, ref):
    from tools.browser_vault_tool import browser_vault_fill

    work.approval_ref = ref
    with _page() as injected, _acp_gate("once") as asked:
        out = json.loads(browser_vault_fill(HANDLE, task_id="t"))
    assert (out["success"], out["error_type"]) == (False, "vault_unavailable")
    assert asked == [] and injected == [] and len(work.resolves()) == 1


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


def _browser_exec_output(stdout: str) -> str:
    """Run the REAL browser_exec result construction (model egress) over a synthetic subprocess stdout."""
    import subprocess

    from tools import browser_use_cli as browser

    with patch.object(browser, "_find_cli", return_value=["synthetic-browser"]), \
         patch.object(browser, "_base_subprocess_env", return_value={}), \
         patch.object(browser, "_resolve_real_profile_cdp", return_value=None), \
         patch.object(browser, "_resolve_backend_cdp", return_value=None), \
         patch.object(browser, "_workspace_dir", return_value=None), \
         patch.object(browser, "_read_browser_cfg", return_value={}), \
         patch.object(browser, "_find_screenshot", return_value=None), \
         patch.object(browser.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout, "")):
        out = browser.browser_exec("print(page_info())", task_id="t")
    return out if isinstance(out, str) else json.dumps(out)


_ADDR_CONTROLS = [{"autocomplete": "address-line1", "formIndex": 0, "index": 0, "label": "", "name": "a1", "type": "text"},
                  {"autocomplete": "address-level2", "formIndex": 0, "index": 1, "label": "", "name": "city", "type": "text"},
                  {"autocomplete": "postal-code", "formIndex": 0, "index": 2, "label": "", "name": "zip", "type": "text"}]


def test_harso_address_values_never_reach_a_later_browser_result(weave, admitted_turn):
    """A Harso address is protected like a password: once filled, no later browser output can echo it."""
    from tools.browser_vault_tool import browser_vault_fill

    with _page(origin="https://shop.example", controls=_ADDR_CONTROLS) as injected:
        out = json.loads(browser_vault_fill(ADDR_HANDLE, task_id="t"))
    assert out["success"] is True and "1 Canary Lane" in injected[0]
    egress = _browser_exec_output("DOM text: 1 Canary Lane, Springfield 12345 (US)")
    for value in ("1 Canary Lane", "Springfield", "12345", "(US)"):
        assert value not in egress, value
    # Short tokens are masked as whole tokens only: "US" is hidden, "STATUS" is not mangled.
    assert "STATUS" in _browser_exec_output("STATUS ok")


def test_harso_address_fill_failure_registers_first_and_scrubs_the_error(weave, admitted_turn):
    from tools import browser_vault_tool

    with _page(origin="https://shop.example", controls=_ADDR_CONTROLS), \
         patch.object(browser_vault_tool, "_eval_js_secret",
                      return_value={"success": False, "error": "Uncaught: bad value 1 Canary Lane"}):
        raw = browser_vault_tool.browser_vault_fill(ADDR_HANDLE, task_id="t")
    assert json.loads(raw)["success"] is False and "1 Canary Lane" not in raw
    assert "1 Canary Lane" not in _browser_exec_output("DOM text: 1 Canary Lane")


@pytest.mark.parametrize("password", ["z", "z9", "z9!", "z9!q"])
def test_a_harso_password_of_any_length_never_reaches_a_later_browser_result(weave, admitted_turn, password):
    from tools.browser_vault_tool import browser_vault_fill

    weave.password = password
    with _page() as injected:
        out = json.loads(browser_vault_fill(HANDLE, task_id="t"))
    assert out["success"] is True and password in injected[0]
    assert f"[{password}]" not in _browser_exec_output(f"DOM password=[{password}]")
    # A secret is exact-substring, never token-scoped: glued to other characters it is still hidden.
    assert password not in _browser_exec_output(f"DOM value=ab{password}cd").replace("DOM value=", "")


@pytest.mark.parametrize("password", ["z", "z9", "z9!", "z9!q"])
def test_a_short_password_is_scrubbed_from_a_fill_failure(weave, admitted_turn, password):
    from tools import browser_vault_tool

    weave.password = password
    with _page(), patch.object(browser_vault_tool, "_eval_js_secret",
                               return_value={"success": False, "error": f"Uncaught: bad [{password}]"}):
        raw = browser_vault_tool.browser_vault_fill(HANDLE, task_id="t")
    assert json.loads(raw)["success"] is False and f"[{password}]" not in raw
    assert f"[{password}]" not in _browser_exec_output(f"DOM password=[{password}]")


_LINE2_CONTROLS = _ADDR_CONTROLS + [{"autocomplete": "address-line2", "formIndex": 0, "index": 3, "label": "",
                                     "name": "a2", "type": "text"}]


@pytest.mark.parametrize("line2", ["7", "9B", "12C", "Apt 4"])
def test_a_short_identifying_address_field_never_reaches_a_later_browser_result(weave, admitted_turn, line2):
    from tools.browser_vault_tool import browser_vault_fill

    weave.address = {**weave.address, "address_line2": line2}
    with _page(origin="https://shop.example", controls=_LINE2_CONTROLS) as injected:
        out = json.loads(browser_vault_fill(ADDR_HANDLE, task_id="t"))
    assert out["success"] is True and line2 in injected[0]
    egress = _browser_exec_output(f"DOM apartment=[{line2}] unit {line2}, 1 Canary Lane")
    assert line2 not in egress.replace("«redacted-vault-secret»", ""), egress
    # Token-scoped, not a blanket substring scrub: text that merely contains the short value survives.
    assert "STATUS" in _browser_exec_output("STATUS ok") and "COUNTRY" in _browser_exec_output("COUNTRY")


@pytest.mark.parametrize("line2", ["7", "12C"])
def test_a_short_address_field_is_scrubbed_from_a_fill_failure(weave, admitted_turn, line2):
    from tools import browser_vault_tool

    weave.address = {**weave.address, "address_line2": line2}
    with _page(origin="https://shop.example", controls=_LINE2_CONTROLS), \
         patch.object(browser_vault_tool, "_eval_js_secret",
                      return_value={"success": False, "error": f"Uncaught: bad apartment [{line2}]"}):
        raw = browser_vault_tool.browser_vault_fill(ADDR_HANDLE, task_id="t")
    assert json.loads(raw)["success"] is False and f"[{line2}]" not in raw
    assert f"[{line2}]" not in _browser_exec_output(f"DOM apartment=[{line2}]")


def _fill_line2(weave, line2):
    from tools.browser_vault_tool import browser_vault_fill

    weave.address = {"address_line2": line2}
    controls = [{"autocomplete": "address-line2", "formIndex": 0, "index": 0, "label": "", "name": "unit",
                 "type": "text"}]
    with _page(origin="https://shop.example", controls=controls) as injected:
        out = json.loads(browser_vault_fill(ADDR_HANDLE, task_id="t"))
    assert out["success"] is True and line2 in injected[0]


@pytest.mark.parametrize("line2", ["7", "9B", "12C", "123D"])
@pytest.mark.parametrize("serialized", [False, True])
def test_a_short_address_line_in_serialized_browser_exec_output_is_scrubbed(weave, admitted_turn, line2, serialized):
    """JSON text puts a value alone on a line as ``\\n12C``: the escape is a boundary, not a glued word char."""
    _fill_line2(weave, line2)
    text = f"Apartment\n{line2}\nShipping\t{line2}"
    egress = _browser_exec_output(json.dumps({"result": text}) if serialized else text)
    assert line2 not in json.loads(egress)["output"], egress


@pytest.mark.parametrize("line2", ["7", "9B", "12C", "123D"])
@pytest.mark.parametrize("serialized", [False, True])
def test_a_short_address_line_in_serialized_cdp_output_is_scrubbed(weave, admitted_turn, line2, serialized):
    from tools import browser_cdp_tool as cdp

    _fill_line2(weave, line2)
    text = f"Apartment\n{line2}\nShipping"
    expression = "JSON.stringify(document.body.innerText)" if serialized else "document.body.innerText"

    async def exchange(endpoint, method, params, target_id, timeout):
        assert method == "Runtime.evaluate" and params["expression"] == expression
        return {"result": {"type": "string", "value": json.dumps(text) if serialized else text}}

    with patch.object(cdp, "_resolve_cdp_endpoint", return_value="ws://127.0.0.1:1/synthetic"), \
         patch.object(cdp, "_browser_cdp_private_guard", return_value=None), \
         patch.object(cdp, "_cdp_call", side_effect=exchange):
        egress = cdp.browser_cdp("Runtime.evaluate", params={"expression": expression, "returnByValue": True})
    assert line2 not in json.loads(egress)["result"]["result"]["value"], egress


def test_escape_boundary_keeps_the_short_token_collision_controls():
    from agent.redact import redact_registered_vault_values, register_vault_redaction_value

    register_vault_redaction_value("US", whole_token=True)
    serialized = json.dumps("STATUS\nCOUNTRY\tBUS\nUS\u00a0US")  # -> ...\nUS\u00a0US (ASCII escapes)
    assert "\\u00a0US" in serialized
    out = redact_registered_vault_values(serialized)
    assert "STATUS" in out and "COUNTRY" in out and "BUS" in out
    assert out.count("«redacted-vault-secret»") == 2, out


def test_a_token_registration_never_downgrades_an_exact_secret():
    from agent.redact import redact_registered_vault_values, register_vault_redaction_value

    register_vault_redaction_value("12C")
    register_vault_redaction_value("12C", whole_token=True)
    assert "12C" not in redact_registered_vault_values("x12Cy")


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


def test_otp_approval_denial_is_the_answer_the_user_is_not_asked_instead(weave, admitted_turn):
    from agent.vault_backends import unlock as unlock_mod
    from tools.browser_vault_tool import browser_vault_enter_code

    weave.decision = "approval_required"
    asked = []
    unlock_mod.set_code_prompt_callback(lambda site, hint: asked.append(site) or "000000")
    try:
        with _acp_gate("deny"), patch("agent.vault_backends.unlock.can_prompt_here", return_value=True), \
             _page(controls=[{"index": 0, "type": "text", "name": "otp", "autocomplete": "one-time-code"}]) as injected:
            out = json.loads(browser_vault_enter_code(HANDLE, task_id="t"))
    finally:
        unlock_mod.set_code_prompt_callback(None)
    assert (out["success"], out["error_type"]) == (False, "denied")
    assert asked == [] and injected == []


_OTP_CONTROLS = [{"index": 0, "type": "text", "name": "otp", "autocomplete": "one-time-code"}]


@contextmanager
def _code_prompt():
    """A surface that CAN ask the user for a code: a failure mistaken for "no key" would ask it and fill the answer."""
    from agent.vault_backends import unlock as unlock_mod

    asked: list = []
    unlock_mod.set_code_prompt_callback(lambda site, hint: asked.append(site) or "000000")
    try:
        with patch("agent.vault_backends.unlock.can_prompt_here", return_value=True):
            yield asked
    finally:
        unlock_mod.set_code_prompt_callback(None)


def _gate_fault(work, fault):
    """One Work-gate failure: what the authority or the attempt's gate does between the card and the value."""
    if fault == "invalid_ref":
        work.approval_ref = "invalid"
    elif fault == "still_pending":
        work.decisions = ["approval_required", "approval_required"]

    def gate(*args, **kwargs):
        if fault == "gate_raises":
            raise RuntimeError("callback failure carrying text the model must not see")
        if fault == "gate_returns_none":
            return None
        if fault == "second_http_503":
            work.force_resolve = (503, {"error": {"code": "UNAVAILABLE", "message": "down"}})
        if fault == "second_not_json":
            work.force_resolve = (200, "not an object")
        if fault == "second_not_digits":
            work.force_resolve = (200, {"decision": "once", "action": "enter_otp", "otp": "12ab56"})
        if fault == "second_wrong_action":
            work.force_resolve = (200, {"decision": "once", "action": "fill_login", "otp": OTP})
        return {"approved": True, "message": None}

    return gate


@pytest.mark.parametrize("fault", ["invalid_ref", "still_pending", "gate_raises", "gate_returns_none", "second_http_503",
                                   "second_not_json", "second_not_digits", "second_wrong_action"])
def test_a_work_code_failure_is_the_answer_never_a_prompt_for_a_code(work, fault):
    """An item WITH an authenticator key: any failure between the Work card and the code is a typed refusal
    (vault_unavailable, or denied when the gate gives no allow). It is never read as "no key", so nobody is asked
    for a code and no advice to save a key is given (V-4b round 2, F1)."""
    from tools import approval
    from tools.browser_vault_tool import browser_vault_enter_code

    with _page(controls=_OTP_CONTROLS) as injected, _code_prompt() as asked, \
         patch.object(approval, "request_tool_approval", side_effect=_gate_fault(work, fault)):
        raw = browser_vault_enter_code(HANDLE, task_id="t")
    out = json.loads(raw)
    typed = "denied" if fault == "gate_returns_none" else "vault_unavailable"  # no answer from the gate is no allow
    assert (out["success"], out["error_type"]) == (False, typed), out
    assert asked == [] and injected == [] and "authenticator" not in out["error"]
    assert "must not see" not in raw and len(work.resolves()) == (1 if fault in ("invalid_ref", "gate_raises",
                                                                              "gate_returns_none") else 2)


@pytest.mark.parametrize("fault", ["invalid_ref", "still_pending", "gate_raises", "second_http_503"])
def test_a_work_code_failure_is_the_answer_on_a_headless_surface_too(work, fault):
    """The reviewer's four cases where no prompt exists (an ACP Work session): typed, not prompt_unavailable."""
    from tools import approval
    from tools.browser_vault_tool import browser_vault_enter_code

    with _page(controls=_OTP_CONTROLS) as injected, \
         patch("agent.vault_backends.unlock.can_prompt_here", return_value=False), \
         patch.object(approval, "request_tool_approval", side_effect=_gate_fault(work, fault)):
        out = json.loads(browser_vault_enter_code(HANDLE, task_id="t"))
    assert (out["success"], out["error_type"]) == (False, "vault_unavailable") and injected == []


@pytest.mark.parametrize("force", [(503, {"error": {"code": "UNAVAILABLE", "message": "down"}}), (200, ["not", "dict"]),
                                   (200, {"decision": "once", "action": "enter_otp", "otp": ""})])
def test_a_cell_code_failure_is_the_answer_never_a_prompt_for_a_code(weave, admitted_turn, force):
    """Same class on a cell turn's first (only) resolve: the vault failing is not the item lacking a key."""
    from tools.browser_vault_tool import browser_vault_enter_code

    weave.force_resolve = force
    with _page(controls=_OTP_CONTROLS) as injected, _code_prompt() as asked:
        out = json.loads(browser_vault_enter_code(HANDLE, task_id="t"))
    assert (out["success"], out["error_type"]) == (False, "vault_unavailable") and asked == [] and injected == []


def test_the_authority_saying_no_usable_key_still_asks_the_user(weave, admitted_turn):
    """The one refusal that DOES mean "no code from the vault": the user is asked, as before."""
    from tools.browser_vault_tool import browser_vault_enter_code

    weave.force_resolve = (409, {"error": {"code": "VAULT_OTP_UNAVAILABLE", "message": "no key"}})
    with _page(controls=_OTP_CONTROLS) as injected, _code_prompt() as asked:
        out = json.loads(browser_vault_enter_code(HANDLE, task_id="t"))
    assert out["success"] is True and asked == ["www.amazon.com"] and "000000" in injected[0]


@pytest.mark.parametrize("kind", ["login", "address"])
@pytest.mark.parametrize("fault", ["gate_raises", "gate_returns_none"])
def test_a_work_gate_that_fails_fills_nothing_on_every_fill_path(work, kind, fault):
    """Sibling of the code path: a gate that raises or answers nothing is a typed, content-free refusal."""
    from tools import approval
    from tools.browser_vault_tool import browser_vault_fill

    handle, origin, controls = HANDLE, ORIGIN, _CONTROLS
    if kind == "address":
        handle, origin = ADDR_HANDLE, "https://shop.example"
        controls = [{"index": 0, "type": "text", "name": "city", "autocomplete": "address-level2"}]
    with _page(origin, controls) as injected, \
         patch.object(approval, "request_tool_approval", side_effect=_gate_fault(work, fault)):
        raw = browser_vault_fill(handle, task_id="t")
    out = json.loads(raw)
    assert (out["success"], out["error_type"]) == ((False, "vault_unavailable") if fault == "gate_raises"
                                                   else (False, "denied"))
    assert injected == [] and "must not see" not in raw and len(work.resolves()) == 1


_OTP_BOX = [{"index": 0, "type": "text", "name": "otp", "autocomplete": "one-time-code"}]


def test_a_login_without_an_authenticator_key_still_asks_harso_for_the_code(weave, admitted_turn):
    """V-otp: Harso reads a seedless login's code from the owner's mail, so the tool must ask it. Regression: the
    V5 has_otp gate skipped the resolve and sent a seedless login straight to the user prompt."""
    from agent.vault_backends import unlock as unlock_mod
    from tools.browser_vault_tool import browser_vault_enter_code

    weave.items[ITEM]["has_otp"] = False
    asked = []
    unlock_mod.set_code_prompt_callback(lambda site, hint: asked.append(site) or "000000")
    try:
        with patch("agent.vault_backends.unlock.can_prompt_here", return_value=True), \
             _page(controls=_OTP_BOX) as injected:
            raw = browser_vault_enter_code(HANDLE, task_id="t")
    finally:
        unlock_mod.set_code_prompt_callback(None)
    out = json.loads(raw)
    assert (out["success"], out["source"], asked) == (True, "weave", [])
    assert [r["body"] for r in weave.resolves()] == [{"handle": HANDLE, "action": "enter_otp", "page_origin": ORIGIN,
                                                      "conversation_id": CONV, "run_id": NATIVE_REF}]
    assert OTP in injected[0] and OTP not in raw


def test_no_mailed_code_hands_the_seedless_login_to_the_user(weave, admitted_turn):
    """otp_unavailable (no code in the owner's mail) keeps the V5 take-over: the user is asked for the code."""
    from agent.vault_backends import unlock as unlock_mod
    from tools.browser_vault_tool import browser_vault_enter_code

    weave.items[ITEM]["has_otp"] = False
    weave.force_resolve = (409, {"error": {"code": "VAULT_OTP_UNAVAILABLE", "message": "No sign-in code is available"}})
    asked = []
    unlock_mod.set_code_prompt_callback(lambda site, hint: asked.append(site) or "246810")
    try:
        with patch("agent.vault_backends.unlock.can_prompt_here", return_value=True), \
             _page(controls=_OTP_BOX) as injected:
            out = json.loads(browser_vault_enter_code(HANDLE, task_id="t"))
        with patch("agent.vault_backends.unlock.can_prompt_here", return_value=False), _page(controls=_OTP_BOX):
            headless = json.loads(browser_vault_enter_code(HANDLE, task_id="t"))
    finally:
        unlock_mod.set_code_prompt_callback(None)
    assert (out["success"], out["source"], asked) == (True, "user", ["www.amazon.com"])
    assert "246810" in injected[0] and len(weave.resolves()) == 2
    assert headless["error_type"] == "prompt_unavailable"


def test_a_non_login_harso_item_is_never_resolved_for_a_code(weave, admitted_turn):
    from tools.browser_vault_tool import browser_vault_enter_code

    with patch("agent.vault_backends.unlock.can_prompt_here", return_value=False), \
         _page(origin="https://shop.example", controls=_OTP_BOX):
        out = json.loads(browser_vault_enter_code(ADDR_HANDLE, task_id="t"))
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


# ---------------------------------------------------------------------------------------------------------------
# Runtime bearer: validated before the transport ever sees it
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("good", [CELL_BEARER, ATTEMPT_BEARER])
@pytest.mark.parametrize("damage", ["\n", " ", "\r\n", "x", "leading"])
def test_a_malformed_bearer_is_refused_before_httpx_and_never_logged(api, monkeypatch, caplog, good, damage):
    """weave-api's presenter shapes, exactly: a damaged bearer is refused, not trimmed, and never reaches HTTPX
    (httpcore's DEBUG log echoes a rejected header value)."""
    from agent.vault_backends.base import VaultUnavailable
    from agent.vault_backends.weave import WeaveLoginBackend

    bad = damage + good if damage == "leading" else good + damage
    monkeypatch.setenv("WEAVE_API_MCP_BEARER", bad)
    caplog.set_level(logging.DEBUG)
    with patch("httpx.Client", side_effect=AssertionError("the transport must not see a malformed bearer")):
        with pytest.raises(VaultUnavailable, match="malformed") as caught:
            WeaveLoginBackend(api.url).list_items()
    assert good not in str(caught.value) and good not in caplog.text and api.requests == []


@pytest.mark.parametrize("bearer", [CELL_BEARER, ATTEMPT_BEARER])
def test_a_well_formed_bearer_reaches_weave_api_unchanged(api, monkeypatch, bearer):
    from agent.vault_backends.weave import WeaveLoginBackend

    monkeypatch.setenv("WEAVE_API_MCP_BEARER", bearer)
    WeaveLoginBackend(api.url).list_items()
    assert [r["authorization"] for r in api.requests] == [f"Bearer {bearer}"]


# ---------------------------------------------------------------------------------------------------------------
# Timeout: runtime config, applied per call
# ---------------------------------------------------------------------------------------------------------------

def _applied_timeouts(monkeypatch):
    import httpx

    seen: list = []
    real = httpx.Client.__init__

    def spy(self, *args, **kwargs):
        seen.append(kwargs.get("timeout"))
        real(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", spy)
    return seen


def test_the_timeout_comes_from_the_real_config_and_an_edit_applies_to_the_next_call(api, monkeypatch):
    from agent.vault_backends.base import enabled_backends

    monkeypatch.setenv("WEAVE_API_MCP_BEARER", CELL_BEARER)
    seen = _applied_timeouts(monkeypatch)
    _write_config({"vault": {"enabled": True, "backend": "weave", "weave_api_url": api.url}})
    enabled_backends()[0].list_items()
    _write_config({"vault": {"enabled": True, "backend": "weave", "weave_api_url": api.url,
                             "weave_timeout_seconds": 2.5}})
    enabled_backends()[0].list_items()
    assert seen == [10.0, 2.5]


@pytest.mark.parametrize("raw", [0, -1, 121, "5", True, float("nan"), [3]])
def test_an_invalid_timeout_falls_back_to_the_default(raw):
    from agent.vault_backends.weave import timeout_seconds

    assert timeout_seconds(raw) == 10.0
    assert timeout_seconds(None) == 10.0 and timeout_seconds(120) == 120.0 and timeout_seconds(0.5) == 0.5


# ---------------------------------------------------------------------------------------------------------------
# V-7b-3: vault_authorize_write opens the api_key write card and hands back only the approval ref
# ---------------------------------------------------------------------------------------------------------------

# The V-7b-1 pinned vector (weave-cloud services/weave-api/tests/test_vault.py), copied byte for byte.
WRITE_URL = "https://api.stripe.com/v1/charges?expand[]=customer"
WRITE_BODY = b'{"amount":2000,"currency":"sgd"}'
WRITE_BODY_SHA256 = "b521722344e10c11d9973a2b7fc30b0e0f52dda4352bae38089ebbf92dbc8bef"
WRITE_DIGEST = "e66c20164532482bd64f1638ccc424328ad6cff21408ef054779ecbe93471714"
STRIPE = "0199bbbb-0000-7000-8000-00000000000d"
STRIPE_HANDLE = f"wv:{STRIPE}"
CANARY_KEY = "sk_live_canary-3b9e-never-in-output"


@pytest.fixture
def stripe(weave):
    weave.items[STRIPE] = _item(STRIPE, "api_key", ["https://api.stripe.com"])
    return weave


def _authorize(handle=STRIPE_HANDLE, method="POST", url=WRITE_URL, body=WRITE_BODY.decode()):
    from tools.vault_write_tool import vault_authorize_write

    return vault_authorize_write(handle, method, url, body)


def test_an_allowed_write_returns_only_the_card_ref_and_header_and_pins_the_7b1_digest(stripe, admitted_turn):
    with _acp_gate("once") as asked:
        raw = _authorize()
    assert json.loads(raw) == {"approval_ref": CARD, "header": "X-Weave-Vault-Approval"}
    assert asked == [f"plugin_rule:{CARD}"]  # the relay reads the card by this key
    [resolve] = stripe.resolves()
    assert resolve["body"] == {"handle": STRIPE_HANDLE, "action": "inject", "method": "POST", "url": WRITE_URL,
                               "body_sha256": WRITE_BODY_SHA256, "conversation_id": CONV, "run_id": NATIVE_REF}
    # The digest weave-api binds the card to, recomputed from exactly what the fork sent: the pinned literal.
    origin, target = "https://api.stripe.com", resolve["body"]["url"][len("https://api.stripe.com"):]
    assert resolve["body"]["url"].startswith(origin + "/")
    digest = hashlib.sha256(f"POST\n{origin}{target}\n{resolve['body']['body_sha256']}".encode()).hexdigest()
    assert digest == WRITE_DIGEST


def test_an_empty_body_hashes_the_empty_string(stripe, admitted_turn):
    with _acp_gate("once"):
        _authorize(method="DELETE", url="https://api.stripe.com/v1/customers/cus_1", body="")
    assert stripe.resolves()[0]["body"]["body_sha256"] == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize("choice", ["deny", "timeout"])
def test_a_denied_write_gets_no_header_and_is_never_asked_again(stripe, admitted_turn, choice):
    with _acp_gate(choice) as asked:
        raw = _authorize()
    out = json.loads(raw)
    assert (out["success"], out["error_type"]) == (False, "denied") and "Do not retry" in out["error"]
    assert "header" not in out and "approval_ref" not in out and CARD not in raw
    assert asked == [f"plugin_rule:{CARD}"] and len(stripe.resolves()) == 1


def test_a_gate_that_raises_is_content_free_and_sends_nothing(stripe, admitted_turn):
    from tools import approval

    with patch.object(approval, "request_tool_approval", side_effect=RuntimeError("gate text the model must not see")):
        raw = _authorize()
    assert json.loads(raw)["error_type"] == "vault_unavailable" and "must not see" not in raw and CARD not in raw


@pytest.mark.parametrize("status,code,error_type", [
    (409, "VAULT_WRITE_UNAVAILABLE", "write_unavailable"),       # runtime flag vault.write_cards off (default)
    (422, "CONTENT_INVALID", "request_invalid"),                 # GET/HEAD, not an api_key, not the exact form
    (403, "VAULT_ORIGIN_REFUSED", "origin_mismatch"),
    (429, "VAULT_RATE_LIMITED", "rate_limited"),
    (404, "VAULT_ITEM_UNAVAILABLE", "not_found"),
])
def test_an_authority_refusal_is_typed_and_never_raises_a_card(stripe, admitted_turn, status, code, error_type):
    stripe.force_resolve = (status, {"error": {"code": code, "message": "refused"}})
    with _acp_gate("once") as asked:
        out = json.loads(_authorize())
    assert (out["success"], out["error_type"]) == (False, error_type) and "header" not in out
    assert asked == [] and len(stripe.resolves()) == 1


def test_a_work_attempt_gets_work_unsupported_until_v4b(stripe, monkeypatch):
    monkeypatch.setenv("WEAVE_API_MCP_BEARER", ATTEMPT_BEARER)
    stripe.force_resolve = (409, {"error": {"code": "VAULT_WORK_APPROVAL_UNSUPPORTED", "message": "refused"}})
    with _acp_gate("once") as asked:
        out = json.loads(_authorize())
    assert (out["success"], out["error_type"]) == (False, "work_unsupported") and asked == []
    assert "conversation_id" not in stripe.resolves()[0]["body"]  # an attempt is its own run


@pytest.mark.parametrize("answer,expected", [
    # A well-formed card with an extra field: only the ref and the header leave, never the extra.
    ({"decision": "approval_required", "approval_ref": CARD, "choices": ["once", "deny"], "api_key": CANARY_KEY},
     {"approval_ref": CARD, "header": "X-Weave-Vault-Approval"}),
    # Anything but approval_required is not a card, even carrying a valid ref.
    ({"decision": "once", "action": "inject", "approval_ref": CARD, "api_key": CANARY_KEY}, "vault_unavailable"),
    ({"decision": "approval_required", "approval_ref": CANARY_KEY}, "vault_unavailable"),
])
def test_no_key_reaches_a_tool_result_whatever_the_authority_answers(stripe, admitted_turn, caplog, answer, expected):
    caplog.set_level(logging.DEBUG)
    stripe.force_resolve = (200, answer)
    with _acp_gate("once") as asked:
        raw = _authorize()
    assert CANARY_KEY not in raw and CANARY_KEY not in caplog.text and CELL_BEARER not in raw
    out = json.loads(raw)
    if isinstance(expected, dict):
        assert out == expected
    else:
        assert (out["success"], out["error_type"]) == (False, expected) and asked == []


def test_a_turn_the_app_did_not_send_opens_no_card(stripe):
    out = json.loads(_authorize())
    assert (out["success"], out["error_type"]) == (False, "no_run") and stripe.resolves() == []


def test_no_handle_lists_only_this_sites_api_keys_as_metadata_and_opens_no_card(stripe, admitted_turn):
    stripe.items[KEY]["allowed_origins"] = ["https://api.example"]  # another site's key: not offered
    totp = "0199bbbb-0000-7000-8000-00000000000e"
    stripe.items[totp] = _item(totp, "totp", ["https://api.stripe.com"])
    for same_site_non_key in (ITEM, ADDR, totp):  # the kind filter, not the origin, must keep these out
        stripe.items[same_site_non_key]["allowed_origins"] = ["https://api.stripe.com"]
    out = json.loads(_authorize(handle=""))
    assert (out["success"], out["error_type"]) == (False, "handle_required")
    assert out["api_keys"] == [{"handle": STRIPE_HANDLE, "label": "Api_Key", "allowed_origins": ["https://api.stripe.com"]}]
    assert stripe.resolves() == []


def test_a_malformed_handle_never_reaches_the_network(stripe, admitted_turn):
    out = json.loads(_authorize(handle="wv:../../v1/vault/items"))
    assert (out["success"], out["error_type"]) == (False, "not_found") and stripe.requests == []


def test_the_tool_is_offered_only_with_the_harso_vault(api):
    from tools.vault_write_tool import _check_available

    assert _check_available() is False  # no config
    _write_config({"vault": {"enabled": True, "backend": "local"}})
    assert _check_available() is False
    _write_config({"vault": {"enabled": False, "backend": "weave", "weave_api_url": api.url}})
    assert _check_available() is False
    _write_config({"vault": {"enabled": True, "backend": "weave", "weave_api_url": api.url}})
    assert _check_available() is True


def test_a_harso_cell_selection_offers_the_tool_only_with_the_harso_vault(api):
    """The real resolver path over the cell's materialized selection (file/skills/terminal/web, no browser)."""
    import model_tools  # noqa: F401 — tool discovery
    from hermes_cli.tools_config import _get_platform_tools
    from model_tools import get_tool_definitions

    cell = {"platform_toolsets": {"api_server": ["file", "skills", "terminal", "web"]}}

    def offered():
        enabled = sorted(_get_platform_tools(cell, "api_server", include_default_mcp_servers=False))
        defs = get_tool_definitions(enabled_toolsets=enabled, quiet_mode=True, skip_tool_search_assembly=True)
        return "vault_authorize_write" in {d["function"]["name"] for d in defs}

    assert offered() is False
    _write_config({"vault": {"enabled": True, "backend": "weave", "weave_api_url": api.url}})
    assert offered() is True


# Round 2 (F1): every malformed input or authority answer is a typed, content-free result at the tool boundary,
# through the real registry dispatch: no card, no header, no parser or exception text, never a second request.
def _dispatch(**changes):
    import tools.vault_write_tool  # noqa: F401 — registers the tool
    from tools.registry import registry

    args = {"handle": STRIPE_HANDLE, "method": "POST", "url": WRITE_URL, "body": WRITE_BODY.decode(), **changes}
    return registry.dispatch("vault_authorize_write", args)


@pytest.mark.parametrize("changes", [
    {"handle": "", "url": "https://[invalid"},                   # unmatched bracket
    {"handle": "", "url": "https://[::1"},
    {"handle": "", "url": "https://[invalid]/"},                 # not an IPv6 literal
    {"handle": "", "url": "https://example.com\uff0fother/"},    # NFKC-invalid authority
    {"url": "https://[bad/"},                                    # with a handle: never reaches the authority either
    {"body": "\ud800"}, {"body": "\udfff"},                      # unpaired surrogates: not sendable as UTF-8
    {"url": "https://api.stripe.com/v1/\ud800"},                 # ...nor in the URL or method sent to the authority
    {"method": "PO\udfffST"}, {"handle": "", "url": "https://api.stripe.com/\udfff"},
])
def test_a_request_that_cannot_be_sent_is_request_invalid_and_sends_nothing(stripe, admitted_turn, changes):
    with _acp_gate("once") as asked:
        raw = _dispatch(**changes)
    out = json.loads(raw)
    assert (out["success"], out["error_type"]) == (False, "request_invalid") and "header" not in out
    assert "Error" not in raw and "example.com" not in raw and "[" not in out["error"]
    assert asked == [] and stripe.requests == []


@pytest.mark.parametrize("handle,force,resolve", [
    (STRIPE_HANDLE, None, (503, {"error": "service unavailable"})),
    (STRIPE_HANDLE, None, (503, {"error": ["service unavailable"]})),
    (STRIPE_HANDLE, None, (503, {"error": 1})),
    (STRIPE_HANDLE, None, (503, {"error": {"code": ["CONTENT_INVALID"]}})),
    (STRIPE_HANDLE, None, (503, {"error": {"code": {"value": "CONTENT_INVALID"}}})),
    ("", (200, {"items": [_item(STRIPE, "api_key", ["https://api.stripe.com"]) | {"allowed_origins": 1}]}), None),
    ("", (200, {"items": [_item(STRIPE, "api_key", ["https://api.stripe.com"]) | {"allowed_origins": True}]}), None),
])
def test_a_malformed_authority_answer_is_vault_unavailable_and_content_free(stripe, admitted_turn, handle, force,
                                                                               resolve):
    stripe.force, stripe.force_resolve = force, resolve
    with _acp_gate("once") as asked:
        raw = _dispatch(handle=handle)
    out = json.loads(raw)
    assert (out["success"], out["error_type"]) == (False, "vault_unavailable") and "header" not in out
    assert "Error" not in raw and "service unavailable" not in raw and "CONTENT_INVALID" not in raw
    assert asked == [] and len(stripe.requests) == 1


def test_a_backend_that_cannot_be_selected_is_vault_unavailable(stripe, admitted_turn):
    from agent.vault_backends import base

    with patch.object(base, "enabled_backends", side_effect=RuntimeError("config text the model must not see")):
        raw = _dispatch()
    assert json.loads(raw)["error_type"] == "vault_unavailable" and "must not see" not in raw
    assert stripe.requests == []


@pytest.mark.parametrize("error", ["down", ["down"], 1, {"code": ["VAULT_ITEM_UNAVAILABLE"]}, {"code": {"x": 1}}])
def test_a_malformed_error_envelope_is_unavailable_on_every_backend_path(api, monkeypatch, error):
    """The shared ``_call`` parser: fill's get_meta and list_items see the same typed failure as the write tool."""
    from agent.vault_backends.base import VaultUnavailable
    from agent.vault_backends.weave import WeaveLoginBackend

    monkeypatch.setenv("WEAVE_API_MCP_BEARER", ATTEMPT_BEARER)
    api.force = (503, {"error": error})
    backend = WeaveLoginBackend(api.url)
    for call in (lambda: backend.get_meta(HANDLE), backend.list_items):
        with pytest.raises(VaultUnavailable, match=r"^the Harso vault answered HTTP 503$"):
            call()
