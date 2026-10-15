"""Harso password source for native browser_vault; restored from 1030559b44^.

Only cell login metadata, passwords and server-minted TOTP codes cross this seam.
No cache, local save, address, payment, API key or retired Work presenter support.
The server binds native session/turn context and rechecks origin/grants/limits.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from agent.vault_backends.base import LoginBackend, VaultUnavailable, VaultUseRefused
from agent.vault_store import VaultItemMeta

_DEFAULT_TIMEOUT_S = 10.0
# Cell presenter shape (ledger_mcp.CONNECTOR_BEARER). A bearer that does not
# match is refused before HTTPX sees it: a malformed header value is echoed by httpcore's DEBUG logging.
_BEARER = re.compile(r"^wvc1_[A-Za-z0-9_-]{43}$")
_REF = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_HANDLE = re.compile(r"^wv:" + _REF.pattern[1:])
_FILLABLE = ("login",)

# weave-api error code -> (tool error_type, what the model is told). Anything else is VaultUnavailable.
_REFUSALS = {
    "VAULT_ORIGIN_REFUSED": ("origin_mismatch", "This vault item is not saved for this site. Nothing was filled."),
    "VAULT_ACTION_REFUSED": ("action_refused", "This vault item cannot be used for this. Nothing was filled."),
    "VAULT_RATE_LIMITED": ("rate_limited", "The vault is refusing more uses for now. Try again later."),
    "VAULT_USE_STALE": ("item_changed", "The user changed this vault item while it was in use. Call "
                                        "browser_vault action=list and try again."),
    "VAULT_OTP_UNAVAILABLE": ("otp_unavailable", "This vault item has no usable authenticator key."),
    "VAULT_ITEM_UNAVAILABLE": ("not_found", "No such vault item. Use browser_vault action=list."),
}
_DENIED = ("The user did not allow this use of the vault item. Nothing was filled. Do not retry it, and do not ask "
           "for the password in chat.")

def _bearer() -> str:
    """The runtime's bearer, exactly as held. Absent -> ""; present but malformed -> refused, never trimmed or
    repaired, and never handed to the transport (content-free error)."""
    from agent.secret_scope import get_secret

    bearer = get_secret("WEAVE_API_MCP_BEARER", "") or ""
    if bearer and not _BEARER.fullmatch(bearer):
        raise VaultUnavailable("the runtime bearer for the Harso vault is malformed")
    return bearer


def _run_context() -> dict[str, str]:
    """Native tool context, not model arguments; the server binds it to this cell's chat."""
    from gateway.native_bot_admission import native_turn_context

    marker = native_turn_context.get() or {}
    session, turn = marker.get("session_id"), marker.get("run_id")
    if not isinstance(session, str) or not session or not isinstance(turn, str) or not _REF.fullmatch(turn):
        raise VaultUseRefused("no_run", "The vault can only be used in a turn sent from the Harso app.")
    return {"session_id": session, "run_id": turn}


def _card_gate(approval_ref: Any, tool: str, description: str, denied: str) -> str:
    """Ask this runtime's approval gate about one Ledger card, keyed by its ref; return the ref on an explicit allow.

    The native stream projects ``plugin_rule:<approval_ref>``. The existing app card flow settles that
    exact Ledger card before waking the native gate. A native allow grants nothing by itself: the second
    server resolve must see a settled card (or Always grant) before releasing a value.
    """
    from tools.approval import request_tool_approval

    if not isinstance(approval_ref, str) or not _REF.fullmatch(approval_ref):
        raise VaultUnavailable("the Harso vault returned an invalid answer")
    try:
        result = request_tool_approval(tool, description, rule_key=approval_ref)
    except Exception:  # health: allow BLE001 -- content-free approval transport boundary
        raise VaultUnavailable("the approval request for this vault use could not be raised") from None
    if not isinstance(result, dict) or result.get("approved") is not True:
        raise VaultUseRefused("denied", denied)
    return approval_ref


def _read_gate(approval_ref: Any, handle: str, action: str, origin: str) -> None:
    _card_gate(approval_ref, "browser_vault", "Use the saved login on this site?", _DENIED)


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
    can_save = False
    requires_origin = True

    def __init__(self, base_url: str, timeout_s: float = _DEFAULT_TIMEOUT_S):
        self._base = base_url.rstrip("/")
        self._timeout_s = timeout_s

    def _call(self, method: str, path: str, body: Optional[dict[str, Any]] = None,
              refusals: dict[str, tuple] = _REFUSALS) -> dict[str, Any]:
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
        error = payload.get("error") if isinstance(payload, dict) else None  # any other shape is just unavailable
        code = error.get("code") if isinstance(error, dict) else None
        if isinstance(code, str) and code in refusals:
            raise VaultUseRefused(*refusals[code])
        raise VaultUnavailable(f"the Harso vault answered HTTP {response.status_code}")

    # -- metadata --------------------------------------------------------------------------------------------

    def list_items(self) -> list[VaultItemMeta]:
        return [meta for meta in self._items() if meta.kind in _FILLABLE]

    def _items(self) -> list[VaultItemMeta]:
        items = self._call("GET", "/v1/vault/runtime/items").get("items")
        if not isinstance(items, list):
            raise VaultUnavailable("weave vault returned an invalid item list")
        return list(map(_meta, items))

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
        body: dict[str, Any] = {"handle": handle, "action": action, "page_origin": origin, **_run_context()}
        answer = self._call("POST", "/v1/vault/resolve", body)
        if answer.get("decision") == "approval_required":
            _read_gate(answer.get("approval_ref"), handle, action, origin)
            answer = self._call("POST", "/v1/vault/resolve", body)  # exactly once: the card is decided now
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
        """Only called for an item that stores an authenticator key, so every failure here is the answer: it is
        raised as a typed refusal, which the code tool returns as is. Anything else would be read as "no key" and
        the user asked for a code instead (only ``otp_unavailable``, from the authority, means that)."""
        try:
            value = self._resolve(handle, "enter_otp", origin, "otp")
            if not isinstance(value, str) or not value.isdigit():
                raise VaultUnavailable("the Harso vault returned an invalid answer")
        except VaultUseRefused:
            raise
        except VaultUnavailable as exc:
            raise VaultUseRefused("vault_unavailable", str(exc)[:200]) from None
        except Exception:  # health: allow BLE001 -- content-free credential authority boundary
            raise VaultUseRefused("vault_unavailable", "the Harso vault could not answer") from None
        return value
