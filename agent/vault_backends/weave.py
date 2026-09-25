"""Harso vault as a login backend (fork, V5). Handles are ``wv:<uuid7>``.

weave-api is the authority (VAULT-design.md §4): it maps this runtime's bearer to its owner, checks the item's
lifecycle, the exact page origin, an Always grant or a once card, and the rate limits, and audits every use. This
backend only relays. The model never sees a value: list/meta carry metadata only, and a resolve returns the one
value a single fill needs, which the browser tool injects and registers for redaction. Nothing is cached, persisted
or logged here, and no error raised here carries a value, a bearer or a response body.

Routes (weave-cloud ``services/weave-api/weave_api/vault.py``, V3):

* ``GET  /v1/vault/runtime/items``            -> list_items
* ``GET  /v1/vault/runtime/items/{handle}``   -> get_meta
* ``POST /v1/vault/resolve``                  -> resolve_password (fill_login), resolve_secret (fill_address),
                                                 resolve_otp (enter_otp: the code is minted server-side; the seed
                                                 never leaves weave-api)

Bearer: ``WEAVE_API_MCP_BEARER``, the per-cell ``wvc1_`` (or a Work attempt's ``wva1_``) the runtime already holds.
A cell names the conversation it serves and the run (the admitted native submit's ``native_request_ref``) a once
card is bound to; a Work attempt is its own run and sends neither.

Saving a login does NOT go through here: the owner routes take the user's session, never a runtime bearer, so a
password typed in the runtime could only reach weave-api by passing through the cell. ``can_save`` is False and the
tool sends the user to the Harso app instead of asking for a password (§5).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from agent.vault_backends.base import LoginBackend, VaultUnavailable, VaultUseRefused
from agent.vault_store import VaultItemMeta

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 10.0
_MAX_TIMEOUT_S = 120.0
# weave-api's own presenter shapes (ledger_mcp.CONNECTOR_BEARER / ATTEMPT_CONNECTOR_BEARER). A bearer that does not
# match is refused before HTTPX sees it: a malformed header value is echoed by httpcore's DEBUG logging.
_BEARER = re.compile(r"^(?:wvc1|wva1)_[A-Za-z0-9_-]{43}$")
_HANDLE = re.compile(r"^wv:[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_SESSION = re.compile(r"^weave-([0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$")
_FILLABLE = ("login", "address", "totp")  # api_key is injected by the egress proxy (V7), never resolved here

# weave-api error code -> (tool error_type, what the model is told). Anything else is VaultUnavailable.
_REFUSALS = {
    "VAULT_ORIGIN_REFUSED": ("origin_mismatch", "This vault item is not saved for this site. Nothing was filled."),
    "VAULT_ACTION_REFUSED": ("action_refused", "This vault item cannot be used for this. Nothing was filled."),
    "VAULT_RATE_LIMITED": ("rate_limited", "The vault is refusing more uses for now. Try again later."),
    "VAULT_USE_STALE": ("item_changed", "The user changed this vault item while it was in use. Call "
                                        "browser_vault_list and try again."),
    "VAULT_OTP_UNAVAILABLE": ("otp_unavailable", "This vault item has no usable authenticator key."),
    "VAULT_ITEM_UNAVAILABLE": ("not_found", "No such vault item. Use browser_vault_list."),
}
_APPROVAL = ("The user has not allowed this use yet. A request is waiting in their Harso app; tell them, "
             "and call this tool again after they allow it. Do not ask for the password in chat.")


def _bearer() -> str:
    """The runtime's bearer, exactly as held. Absent -> ""; present but malformed -> refused, never trimmed or
    repaired, and never handed to the transport (content-free error)."""
    from agent.secret_scope import get_secret

    bearer = get_secret("WEAVE_API_MCP_BEARER", "") or ""
    if bearer and not _BEARER.fullmatch(bearer):
        raise VaultUnavailable("the runtime bearer for the Harso vault is malformed")
    return bearer


def timeout_seconds(raw: Any) -> float:
    """``vault.weave_timeout_seconds``: a number in (0, 120]. Anything else is the default (logged, content-free)."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 < raw <= _MAX_TIMEOUT_S:
        if raw is not None:
            logger.warning("vault.weave_timeout_seconds must be a number in (0, %s]; using %s",
                           _MAX_TIMEOUT_S, _DEFAULT_TIMEOUT_S)
        return _DEFAULT_TIMEOUT_S
    return float(raw)


def _run_context(bearer: str) -> tuple[Optional[str], Optional[str]]:
    """``(conversation_id, run_id)`` for a resolve. A Work attempt (``wva1_``) is its own run: both None.

    A cell turn's run is the native submit that named it (``agent._current_turn_id`` is weave-api's command id,
    staged by gateway/run.py); its ``native_request_ref`` is what the Ledger recorded for the turn, and its
    conversation is the lineage root session ``weave-<conversation_id>``. A turn weave-api did not send has no
    run, so no once card can ever be spent by it.
    """
    if bearer.startswith("wva1_"):
        return None, None
    from hermes_state import SessionDB
    from tools.approval import get_current_turn_id

    turn_id = get_current_turn_id()
    run = None
    if turn_id:
        try:
            db = SessionDB(read_only=True)
        except Exception:
            db = None  # no state.db yet: no turn was ever admitted here, so there is no run
        if db is not None:
            try:
                run = db.native_submit_run(turn_id)
            finally:
                db.close()
    match = _SESSION.fullmatch(run["root_session_id"]) if run else None
    if run is None or match is None:
        raise VaultUseRefused("no_run", "The vault can only be used in a turn sent from the Harso app.")
    return match.group(1), run["native_request_ref"]


def _meta(raw: Any) -> VaultItemMeta:
    if not isinstance(raw, dict) or not _HANDLE.fullmatch(str(raw.get("id") or "")):
        raise VaultUnavailable("weave vault returned an invalid item")
    origins = tuple(o for o in (raw.get("allowed_origins") or ()) if isinstance(o, str))
    return VaultItemMeta(
        id=raw["id"], kind=str(raw.get("kind") or ""), label=str(raw.get("label") or ""),
        origin=origins[0] if origins else None, created_at=str(raw.get("created_at") or ""),
        identifier_type=raw.get("identifier_type") if raw.get("identifier") else None,
        identifier=raw.get("identifier") or None, has_otp=raw.get("has_otp") is True, allowed_origins=origins)


class WeaveLoginBackend(LoginBackend):
    name = "weave"
    display_name = "Harso vault"
    prefix = "wv:"
    protects_all_values = True

    def __init__(self, base_url: str, timeout_s: float = _DEFAULT_TIMEOUT_S):
        self._base = base_url.rstrip("/")
        self._timeout_s = timeout_s

    def _call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        import httpx

        bearer = _bearer()
        if not self._base.startswith(("https://", "http://")) or not bearer:
            raise VaultUnavailable("the Harso vault is not configured in this session")
        try:
            with httpx.Client(timeout=self._timeout_s, follow_redirects=False) as client:
                response = client.request(method, self._base + path, json=body,
                                          headers={"Authorization": f"Bearer {bearer}"})
        except httpx.HTTPError as exc:
            # Only the class name: an httpx message can carry the URL, never the bearer, but stay minimal.
            raise VaultUnavailable(f"the Harso vault is unreachable ({type(exc).__name__})") from None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code == 200 and isinstance(payload, dict):
            return payload
        code = ((payload or {}).get("error") or {}).get("code") if isinstance(payload, dict) else None
        if code in _REFUSALS:
            raise VaultUseRefused(*_REFUSALS[code])
        raise VaultUnavailable(f"the Harso vault answered HTTP {response.status_code}")

    # -- metadata --------------------------------------------------------------------------------------------

    def list_items(self) -> List[VaultItemMeta]:
        items = self._call("GET", "/v1/vault/runtime/items").get("items")
        if not isinstance(items, list):
            raise VaultUnavailable("weave vault returned an invalid item list")
        return [meta for meta in map(_meta, items) if meta.kind in _FILLABLE]

    def get_meta(self, handle: str) -> Optional[VaultItemMeta]:
        if not _HANDLE.fullmatch(handle or ""):
            return None
        try:
            meta = _meta(self._call("GET", f"/v1/vault/runtime/items/{handle}"))
        except VaultUseRefused as refusal:
            if refusal.error_type == "not_found":
                return None
            raise
        return meta if meta.id == handle and meta.kind in _FILLABLE else None

    # -- one use, by handle, for one exact origin ------------------------------------------------------------

    def _resolve(self, handle: str, action: str, origin: Optional[str], field: str) -> Any:
        if not _HANDLE.fullmatch(handle or "") or not origin:
            raise VaultUseRefused("origin_required", "A vault item is only used on the page it is saved for.")
        bearer = _bearer()
        conversation_id, run_id = _run_context(bearer)
        body: Dict[str, Any] = {"handle": handle, "action": action, "page_origin": origin}
        if conversation_id is not None:
            body.update(conversation_id=conversation_id, run_id=run_id)
        answer = self._call("POST", "/v1/vault/resolve", body)
        if answer.get("decision") == "approval_required":
            raise VaultUseRefused("approval_required", _APPROVAL)
        value = answer.get(field)
        if answer.get("decision") not in ("once", "always") or answer.get("action") != action or not value:
            raise VaultUnavailable("the Harso vault returned an invalid answer")
        return value

    def resolve_password(self, handle: str, *, origin: Optional[str] = None) -> str:
        value = self._resolve(handle, "fill_login", origin, "password")
        if not isinstance(value, str):
            raise VaultUnavailable("the Harso vault returned an invalid answer")
        return value

    def resolve_otp(self, handle: str, *, origin: Optional[str] = None) -> Optional[str]:
        value = self._resolve(handle, "enter_otp", origin, "otp")
        if not isinstance(value, str) or not value.isdigit():
            raise VaultUnavailable("the Harso vault returned an invalid answer")
        return value

    def resolve_secret(self, handle: str, *, origin: Optional[str] = None) -> Dict[str, str]:
        """Address fields only: Harso stores no payment cards (founder ruling), and a login fills by password."""
        value = self._resolve(handle, "fill_address", origin, "fields")
        if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
            raise VaultUnavailable("the Harso vault returned an invalid answer")
        return dict(value)
