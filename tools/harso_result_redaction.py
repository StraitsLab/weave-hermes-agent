"""Vault result egress boundary retained from the Harso cell fork."""
import json
import re


def redact_registered_vault_values(text):
    from agent import redact
    from tools.harso_vault_encodings import _encoded_forms
    with redact._VAULT_REDACTION_LOCK:
        values = list((redact._VAULT_REDACTION_VALUES.get(redact._vault_scope()) or {}).items())
    for value, whole_token in sorted(values, key=lambda item: len(item[0]), reverse=True):
        if whole_token is True:  # S4 protects short non-secret fields only at token boundaries.
            text = redact.redact_registered_vault_values(text)
            continue
        if len(value) <= len(text):
            for form in _encoded_forms(value):
                text = text.replace(form, "«redacted-vault-secret»")
    return text

_INLINE_IMAGE_URL = re.compile(r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/]*={0,2}")


def _inline_image_url_path(part) -> tuple:
    """The one opaque path of an inline base64 image part: ``image_url.url`` when it is exactly a data URL."""
    image = part.get("image_url") if isinstance(part, dict) and part.get("type") == "image_url" else None
    if isinstance(image, dict) and isinstance(image.get("url"), str) and _INLINE_IMAGE_URL.fullmatch(image["url"]):
        return (("image_url", "url"),)
    return ()


def _scrub_vault_values(result, *, tool: str = ""):
    """Vault secrets are a hard model-egress boundary: every tool result, whatever tool or field produced it
    (a page title, a URL after a form GET, an exception), is scrubbed of registered fill values. Opaque bytes
    stay byte-identical only where their owner declares them: the data URL of an inline ``data:image/...;base64``
    part of the handler's own multimodal envelope, and the protocol-defined binary paths of a ``browser_cdp``
    result."""
    if isinstance(result, dict) and result.get("_multimodal") is True and isinstance(result.get("content"), list):
        return {_scrub_json(k): [_scrub_json(p, _inline_image_url_path(p)) for p in v] if k == "content"
                else _scrub_json(v) for k, v in result.items()}
    if isinstance(result, str):
        return _scrub_wire(result, tool)
    return _scrub_json(result)


def _scrub_wire(text: str, tool: str = "") -> str:
    """The handler's top-level string result. A JSON document is parsed first (before any raw-form check, so
    an escaped spelling cannot hide a value) and only its string keys and values are scrubbed, so a value that
    collides with JSON syntax (``true``, ``123456``, a backslash) never rewrites framing. Unchanged text is
    returned byte-identical."""
    from agent.redact import _VAULT_REDACTION_VALUES, _vault_scope

    if not _VAULT_REDACTION_VALUES.get(_vault_scope()):  # nothing registered: byte-identical, no parse
        return text
    if text.lstrip()[:1] in ("{", "["):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            always = flagged = ()
            if tool == "browser_cdp" and isinstance(parsed, dict) and isinstance(parsed.get("method"), str):
                # browser_cdp_tool's own sets and base64Encoded rule, only on its carrier paths (never ambient
                # trust in a flag elsewhere, which page-controlled JSON could spoof)
                from tools.browser_cdp_tool import _CDP_ALWAYS_BINARY_PATHS, _CDP_FLAGGED_BINARY_PATHS

                always = tuple(("result",) + p for p in _CDP_ALWAYS_BINARY_PATHS.get(parsed["method"], ()))
                flagged = tuple(("result",) + p for p in _CDP_FLAGGED_BINARY_PATHS.get(parsed["method"], ()))
            scrubbed = _scrub_json(parsed, always, flagged)
            return text if scrubbed == parsed else json.dumps(scrubbed, ensure_ascii=False)
    return redact_registered_vault_values(text)


def _scrub_json(value, always: tuple = (), flagged: tuple = ()):
    """String leaves and keys are scrubbed as TEXT, never re-parsed as new JSON documents; numbers, booleans
    and null are never touched."""

    if isinstance(value, str):
        return redact_registered_vault_values(value)
    if isinstance(value, list):
        return [_scrub_json(v) for v in value]
    if not isinstance(value, dict):
        return value
    base64_flagged = value.get("base64Encoded") is True
    out = {}
    for key, item in value.items():
        if isinstance(item, str) and ((key,) in always or (base64_flagged and (key,) in flagged)):
            out[key] = item
        else:
            out[_scrub_json(key)] = _scrub_json(item, tuple(p[1:] for p in always if len(p) > 1 and p[0] == key),
                                                tuple(p[1:] for p in flagged if len(p) > 1 and p[0] == key))
    return out
