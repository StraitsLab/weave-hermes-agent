"""Bounded step labels from allowlisted argument fields, never raw tool payloads."""

import posixpath
import re
from urllib.parse import urlsplit

_COMMAND_HEAD = re.compile(r"[A-Za-z0-9._/][A-Za-z0-9._/-]{0,63}")
_PLAIN_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9.,:'()/-]{0,23}")


def _command(field: str):
    def preview(args: dict) -> str | None:
        command = args.get(field)
        tokens = command.split() if isinstance(command, str) else []
        if not tokens or not _COMMAND_HEAD.fullmatch(tokens[0]):
            return None
        return tokens[0].rsplit("/", 1)[-1]
    return preview


def _host(url) -> str | None:
    if not isinstance(url, str):
        return None
    try:
        return urlsplit(url).hostname or None
    except ValueError:
        return None


def _first_url(args: dict) -> str | None:
    urls = args.get("urls")
    return _host(urls[0] if isinstance(urls, list) and urls else None)


def _basename(args: dict) -> str | None:
    path = args.get("path")
    return posixpath.basename(path.rstrip("/")) if isinstance(path, str) else None


def _plain_words(goal, limit: int = 6) -> str | None:
    """A free-text goal's first words, only when every one is short plain prose (no keys, ids, paths)."""
    words = goal.split() if isinstance(goal, str) else []
    head = words[:limit]
    if not head or not all(_PLAIN_WORD.fullmatch(word) for word in head):
        return None
    return " ".join(head) + ("…" if len(words) > limit else "")


def _helper(args: dict) -> str | None:
    tasks = args.get("tasks")
    if isinstance(tasks, list) and len(tasks) > 1:
        return f"{len(tasks)} tasks"
    first = tasks[0] if isinstance(tasks, list) and tasks else None
    return _plain_words(first.get("goal") if isinstance(first, dict) else args.get("goal"))


_PREVIEWS = {
    "terminal": _command("command"),
    "process": _command("command"),
    "execute_code": _command("code"),
    "web_extract": _first_url,
    "browser_navigate": lambda args: _host(args.get("url")),
    "read_file": _basename,
    "write_file": _basename,
    "patch": _basename,
    "search_files": _basename,
    "delegate_task": _helper,
}


def step_preview(name: str, args: dict | None) -> str | None:
    preview = _PREVIEWS.get(name) if isinstance(name, str) else None
    text = preview(args) if preview and isinstance(args, dict) else None
    if not isinstance(text, str):
        return None
    from agent.redact import redact_sensitive_text

    text = redact_sensitive_text(" ".join(text.split()), force=True, redact_url_credentials=True)
    return text[:120] or None
