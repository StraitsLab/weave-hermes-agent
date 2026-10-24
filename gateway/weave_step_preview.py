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
    elif name == "delegate_task":
        tasks = args.get("tasks")
        if isinstance(tasks, list) and len(tasks) > 1:
            return f"{len(tasks)} tasks"
        goal = tasks[0].get("goal") if isinstance(tasks, list) and tasks and isinstance(tasks[0], dict) else args.get("goal")
        text = _plain_words(goal)
    if not isinstance(text, str):
        return None
    from agent.redact import redact_sensitive_text

    text = redact_sensitive_text(" ".join(text.split()), force=True, redact_url_credentials=True)
    return text[:120] or None


_PLAIN_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9.,:'()/-]{0,23}")


def _plain_words(goal, limit: int = 6) -> str | None:
    """A free-text goal's first words, only when every one is short plain prose (no keys, ids, paths)."""
    if not isinstance(goal, str):
        return None
    words = goal.split()
    head = words[:limit]
    if not head or not all(_PLAIN_WORD.fullmatch(word) for word in head):
        return None
    return " ".join(head) + ("…" if len(words) > limit else "")
