"""Web Bot Auth signatures on every request the agent's browser sends (fork, AB-2).

Each request gains ``Signature``, ``Signature-Input`` and ``Signature-Agent`` per
draft-meunier-web-bot-auth-architecture-05 section 4.2: covered components ``"@authority"`` and
``"signature-agent"``, tag ``web-bot-auth``, a 64-byte nonce, ``expires`` 60 s after ``created``. The bytes are
those of weave-cloud AB-1 ``infra/web-bot-auth/wba_directory.py sign-request``. ``tools/browser_supervisor.py``
adds them through CDP ``Fetch`` interception.

The private key never enters this process. A plugin answers the ``browser_request_signer`` hook with a signer:
``public_key()`` (32 raw Ed25519 bytes), ``sign(message)`` (the signature) and ``signature_agent`` (the key
directory URL). The Weave cell's plugin (a follow-up card, with its weave-api signing endpoint) will ask weave-api,
which asks KMS. ``browser.web_bot_auth`` (default false) turns signing on. Any failure leaves the request unsigned
and logged; signing never blocks browsing.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
import time
from typing import Dict, Optional
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

logger = logging.getLogger(__name__)

TAG = "web-bot-auth"
VALIDITY_SECONDS = 60
SIGNED_HEADERS = ("Signature", "Signature-Input", "Signature-Agent")
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sf_string(value: str) -> str:
    if not all(0x20 <= ord(char) < 0x7F for char in value):
        raise ValueError("structured-field strings must be printable ASCII")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def jwk_thumbprint(public_key: bytes) -> str:
    """RFC 7638 thumbprint of an Ed25519 OKP key (RFC 8037 appendix A.3): the ``keyid``."""
    canonical = f'{{"crv":"Ed25519","kty":"OKP","x":"{_b64url(public_key)}"}}'
    return _b64url(hashlib.sha256(canonical.encode("ascii")).digest())


def authority_of(url: str) -> str:
    """RFC 9421 section 2.2.3 ``@authority`` as the target server sees it: lowercase host, IPv6 in brackets,
    the scheme's default port omitted. AB-1's rules, extended from https to http."""
    parts = urlsplit(url)
    default_port = _DEFAULT_PORTS.get(parts.scheme)
    if default_port is None or not parts.hostname:
        raise ValueError("only http(s) URLs with a host are signed")
    if re.fullmatch(r"[0-9a-f]*:[0-9a-f:.]*", parts.hostname):
        host = f"[{parts.hostname}]"
    elif re.fullmatch(r"[a-z0-9.-]+", parts.hostname):
        host = parts.hostname
    else:
        raise ValueError("host must be an ASCII DNS name (punycode) or an IP literal")
    return host if parts.port in (None, default_port) else f"{host}:{parts.port}"


class RequestSigner:
    """Signs requests through a plugin signer. Each signature is verified against the signer's public key before
    it is used, so a broken signer yields an unsigned request, never a wrong signature."""

    def __init__(self, signer) -> None:
        public_key = signer.public_key()
        self._verify = Ed25519PublicKey.from_public_bytes(public_key).verify
        self._sign = signer.sign
        self.keyid = jwk_thumbprint(public_key)
        self.agent = _sf_string(signer.signature_agent)

    def headers(self, url: str, *, created: Optional[int] = None, nonce: Optional[str] = None,
                validity_seconds: int = VALIDITY_SECONDS) -> Dict[str, str]:
        created = int(time.time()) if created is None else created
        nonce = base64.b64encode(secrets.token_bytes(64)).decode("ascii") if nonce is None else nonce
        params = (f'("@authority" "signature-agent");created={created};keyid={_sf_string(self.keyid)};'
                  f'alg="ed25519";expires={created + validity_seconds};nonce={_sf_string(nonce)};tag="{TAG}"')
        base = (f'"@authority": {authority_of(url)}\n"signature-agent": {self.agent}\n'
                f'"@signature-params": {params}').encode("ascii")
        signature = self._sign(base)
        self._verify(signature, base)
        return {"Signature-Agent": self.agent, "Signature-Input": f"sig1={params}",
                "Signature": f"sig1=:{base64.b64encode(signature).decode('ascii')}:"}


def web_bot_auth_enabled() -> bool:
    """``browser.web_bot_auth`` is exactly ``true``."""
    try:
        from hermes_cli.config import read_raw_config

        browser = read_raw_config().get("browser") or {}
        return isinstance(browser, dict) and browser.get("web_bot_auth") is True
    except Exception:
        return False


_resolved: Optional[RequestSigner] = None


def request_signer() -> Optional[RequestSigner]:
    """The process's signer while ``browser.web_bot_auth`` is on: resolved once it succeeds, retried (and logged)
    while it does not. A supervisor keeps the signer it started with."""
    global _resolved
    if not web_bot_auth_enabled():
        return None
    if _resolved is None:
        _resolved = resolve_request_signer()
    return _resolved


def resolve_request_signer() -> Optional[RequestSigner]:
    """The first ``browser_request_signer`` answer, else ``None``.

    Never raises: with no signer the browser runs unsigned, and the reason is logged (exception type only, since a
    plugin's error text may carry its bearer)."""
    try:
        from hermes_cli.plugins import PluginManager, has_hook, iter_hook_callbacks

        for callback in iter_hook_callbacks("browser_request_signer") if has_hook("browser_request_signer") else ():
            signer = PluginManager._invoke_hook_callback(callback, {})
            if signer is not None:
                return RequestSigner(signer)
        logger.warning("browser.web_bot_auth is on but no plugin answers browser_request_signer; "
                       "browser requests stay unsigned")
    except Exception as exc:
        logger.warning("web bot auth: no request signer (%s); browser requests stay unsigned", type(exc).__name__)
    return None
