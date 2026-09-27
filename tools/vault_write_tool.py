"""Fork (V-7b-3): ``vault_authorize_write`` asks the user to allow ONE https write that uses a Harso vault API key.

The key never reaches this process. weave-api opens a new once/deny card bound to this exact request (method, URL,
sha256 of the body) and returns only the card's ref. This tool raises the runtime's own approval gate keyed by that
ref, so the chat relay (or a Work attempt's host) shows the card. On an allow it returns the ref and the header name;
the model sends the request with those exact bytes plus that header, and the egress proxy injects the key only if the
Ledger spends that card for that request. A deny or a refusal is the answer: it is returned typed and never retried.

Offered only where the Harso vault is the backend (``vault.enabled: true`` and ``vault.backend: weave``).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


def _check_available() -> bool:
    from agent.vault_backends.base import _cfg
    from tools.browser_vault_tool import _vault_opted_in

    return _vault_opted_in() and _cfg().get("backend") == "weave"


def _refused(error_type: str, error: str, **extra: Any) -> str:
    return json.dumps({"success": False, "error_type": error_type, "error": error, **extra})


def vault_authorize_write(handle: str, method: str, url: str, body: str = "") -> str:
    from agent.vault_backends.base import VaultUnavailable, VaultUseRefused, enabled_backends
    from agent.vault_backends.weave import APPROVAL_HEADER, WeaveLoginBackend

    backend = next((b for b in enabled_backends() if isinstance(b, WeaveLoginBackend)), None)
    if backend is None:
        return _refused("vault_unavailable", "The Harso vault is not configured in this session.")
    try:
        if not handle:  # metadata only: the API key handles saved for this URL's site
            parts = urlsplit(url or "")
            origin = f"{parts.scheme}://{parts.netloc}"
            return _refused("handle_required", "Name one of these API key handles for this site, then call again.",
                            api_keys=[{"handle": m.id, "label": m.label, "allowed_origins": list(m.allowed_origins)}
                                      for m in backend.list_api_keys() if origin in m.allowed_origins])
        ref = backend.authorize_write(handle, method, url, body.encode("utf-8"))
    except VaultUseRefused as refusal:
        return _refused(refusal.error_type, str(refusal))
    except VaultUnavailable as exc:
        return _refused("vault_unavailable", str(exc)[:200])
    return json.dumps({"approval_ref": ref, "header": APPROVAL_HEADER})


VAULT_AUTHORIZE_WRITE_SCHEMA = {
    "name": "vault_authorize_write",
    "description": (
        "Before an https write (POST, PUT, PATCH, DELETE) to an API whose key the user saved in the Harso vault: ask "
        "the user to allow exactly this one request. You never see the key. On an allow you get {approval_ref, "
        "header}: send the request once, through the terminal, with exactly this method, URL and body (byte for byte, "
        "e.g. curl --data-binary) plus the header `<header>: <approval_ref>`, and no Authorization header; the network "
        "proxy adds the key. Any change to the request, or a second send, is refused. Every write asks again. "
        "denied means the user said no: do not retry and do not ask for the key in chat. Call without a handle to "
        "get the API key handles saved for the URL's site."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "handle": {"type": "string", "description": "The API key's vault handle (wv:…)."},
            "method": {"type": "string", "description": "Uppercase HTTP method, e.g. POST. Not GET or HEAD."},
            "url": {"type": "string", "description": ("Exact URL as it will be sent: https://host/path?query, "
                                                      "lowercase host, no port 443, no user, no fragment.")},
            "body": {"type": "string", "description": "Exact request body as it will be sent (UTF-8); empty if none."},
        },
        "required": ["method", "url"],
    },
}


def _handle(args: Dict[str, Any], **kwargs) -> str:
    return vault_authorize_write(str(args.get("handle") or ""), str(args.get("method") or ""),
                                 str(args.get("url") or ""), str(args.get("body") or ""))


from tools.registry import no_cache_check_fn, registry  # noqa: E402

registry.register(
    name="vault_authorize_write",
    toolset="terminal",
    schema=VAULT_AUTHORIZE_WRITE_SCHEMA,
    handler=_handle,
    check_fn=no_cache_check_fn(_check_available),
    emoji="🔐",
)
