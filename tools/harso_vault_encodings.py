"""Literal encoded forms for the Harso vault egress boundary."""
import html
import json
import re
from urllib.parse import quote, quote_plus

def _json_escapes(text: str):
    """``text`` as it reads inside JSON strings nested 1-5 deep, with and without ``\\uXXXX`` for non-ASCII. Five,
    so a JSON document's text holds every form a string inside it can hold 1-4 deep (see vault_value_in)."""
    out, layer = [], {text}
    for _ in range(5):
        layer = {f for t in layer for f in (json.dumps(t)[1:-1], json.dumps(t, ensure_ascii=False)[1:-1])}
        out += layer
    return out

def _encoded_forms(value: str) -> tuple:
    """The concrete encodings a filled value reaches tool output in, longest first: Python repr, JSON string escapes
    (nested, ``\\uXXXX``, every character ``\\u``-escaped), percent-encoding in UTF-8 or windows-1252
    (encodeURIComponent, a form GET to a page without a charset; either hex case; ``+`` or ``%20`` for space)
    and the HTML entities a DOM serializer emits. A finite literal set, matched with ``str.replace``: the cost
    is linear in the output, whatever characters the value holds (no regex is built from the secret)."""
    bases = {value, repr(value)[1:-1], ascii(value)[1:-1],  # Python repr: a KeyError, a %r log argument
             html.escape(value, quote=False), html.escape(value, quote=True),
             html.escape(value, quote=True).replace("&#x27;", "&#39;"),
             value.replace("&", "&amp;").replace('"', "&quot;")}
    for enc in ("utf-8", "cp1252"):
        # encodeURIComponent, urllib's quote/quote_plus defaults, WHATWG form encoding, encodeURI
        for safe in ("", "/", "*", "!~*'()", ";,/?:@&=+$#!~*'()"):
            try:
                quoted = [quote(value, safe=safe, encoding=enc), quote_plus(value, safe=safe, encoding=enc)]
            except UnicodeEncodeError:
                continue
            for q in quoted:
                bases.update((q, re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), q)))
    bases.add("".join(json.dumps(c, ensure_ascii=True)[1:-1] if ord(c) > 0xFFFF else f"\\u{ord(c):04x}" for c in value))
    forms = set(bases)
    for base in bases:
        forms.update(_json_escapes(base))
    forms.update(json.dumps(b, ensure_ascii=a)[1:-1].replace("/", "\\/") for b in bases if "/" in b for a in (0, 1))  # \/
    # \\uXXXX hex in upper case too (a generated form, never text built from the page)
    forms.update([re.sub(r"\\u[0-9a-f]{4}", lambda m: "\\u" + m.group(0)[2:].upper(), f) for f in forms])
    forms.discard("")
    return tuple(sorted(forms, key=len, reverse=True))
