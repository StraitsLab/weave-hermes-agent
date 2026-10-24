"""Bounded step labels from allowlisted argument fields, never raw tool payloads."""

import posixpath
import re
from urllib.parse import urlsplit


def step_preview(name: str, args: dict | None) -> str | None:
    if not isinstance(args, dict) or not isinstance(name, str):
        return None
    text = None
    if name in {"terminal", "execute_code", "process"}:
        command = args.get("code" if name == "execute_code" else "command")
        if not isinstance(command, str):
            return None
        tokens = command.split()
        if not tokens or not re.fullmatch(r"[A-Za-z0-9._/][A-Za-z0-9._/-]{0,63}", tokens[0]):
            return None
        text = tokens[0].rsplit("/", 1)[-1]
    elif name in {"web_extract", "browser_navigate"}:
        if name == "web_extract":
            urls = args.get("urls")
            url = urls[0] if isinstance(urls, list) and urls else None
        else:
            url = args.get("url")
        if not isinstance(url, str):
            return None
        try:
            parsed = urlsplit(url)
            if not parsed.hostname:
                return None
            text = parsed.hostname
        except ValueError:
            return None
    elif name in {"read_file", "write_file", "patch", "search_files"}:
        path = args.get("path")
        if isinstance(path, str):
            text = posixpath.basename(path.rstrip("/"))
    if not isinstance(text, str):
        return None
    from agent.redact import redact_sensitive_text

    text = redact_sensitive_text(" ".join(text.split()), force=True, redact_url_credentials=True)
    return text[:120] or None
