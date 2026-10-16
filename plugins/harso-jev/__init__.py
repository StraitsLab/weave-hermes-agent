"""Bounded Jev goals on Hermes's own browser backend."""
import contextlib
import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA = {
    "name": "browse",
    "description": (
        "Open and read a page, or perform one narrow browser goal on Hermes's browser computer. "
        "Empty goal returns visible page text. expect is independently checked against the live page. "
        "allow_hosts adds comma-separated hosts (the starting host is always allowed). "
        "Pass browser_page.target to resume that exact tab WITHOUT navigating (it goes to url only when the tab "
        "is on another site); text_offset reads more text. allow_hosts may be empty: the start site's sibling hosts "
        "(www., other subdomains) are allowed already. "
        "Without a target, a chat keeps working in its last browser window (one live card); set new_window "
        "true only for separate parallel work that needs its own window and card. "
        "Never supply passwords, payment details or verification codes; use browser_vault tools directly. "
        "Do not bypass approvals, CAPTCHA or site restrictions. Never place an order."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "goal": {"type": "string", "default": ""},
            "expect": {"type": "string", "default": ""},
            "allow_hosts": {"type": "string", "default": ""},
            "target": {"type": "string", "default": ""},
            "text_offset": {"type": "integer", "minimum": 0, "maximum": 2000000, "default": 0},
            "new_window": {"type": "boolean", "default": False},
        },
        "required": ["url"],
        "additionalProperties": False,
    },
}

# Windows Jev opened: {"chats": {session: target}, "windows": {target: last_used_epoch}}. Only these are ever
# reused or closed; a window the person opened is never in the file.
WINDOW_IDLE_SECONDS = 6 * 3600


def _windows_file():
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "cache" / "harso-jev-windows.json"


def _idle_seconds():
    try:
        from hermes_cli.config import load_config_readonly
        value = ((load_config_readonly() or {}).get("harso_jev") or {}).get("window_idle_seconds")
        return value if type(value) is int and 60 <= value <= 7 * 86400 else WINDOW_IDLE_SECONDS
    except Exception:  # health: allow BLE001 -- a bad config keeps the agreed default
        return WINDOW_IDLE_SECONDS


def _with_windows(change):
    """Read-modify-write the window file under a lock; ``change(state)`` returns the value to hand back."""
    import fcntl
    path = _windows_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        try:
            state = json.loads(handle.read() or "{}")
        except ValueError:
            state = {}
        if not isinstance(state.get("chats"), dict) or not isinstance(state.get("windows"), dict):
            state = {"chats": {}, "windows": {}}
        answer = change(state)
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(state))
        return answer


def _plan_windows(session, now, idle):
    """(this chat's window to keep using or "", Jev windows idle past the limit to close now)."""
    def change(state):
        stale = [t for t, at in state["windows"].items() if not isinstance(at, (int, float)) or now - at > idle]
        for target in stale:
            state["windows"].pop(target, None)
        state["chats"] = {s: t for s, t in state["chats"].items() if t in state["windows"]}
        return state["chats"].get(session, ""), stale
    return _with_windows(change)


def _remember_window(session, target, now):
    def change(state):
        state["windows"][target] = now
        state["chats"][session] = target
    _with_windows(change)


def _publish_activity(result, credentials):
    try:
        import httpx
        from agent.vault_backends.weave import _bearer, _run_context

        page = result.get("browser_page")
        base = credentials.get("WEAVE_API_URL", "").rstrip("/")
        if result.get("success") is not True or not isinstance(page, dict) or not base:
            return
        bearer = _bearer()
        if not bearer:
            return
        body = {"session_id": _run_context()["session_id"],
                **{key: page[key] for key in ("target", "url", "title")}}
        with httpx.Client(timeout=3.0, follow_redirects=False) as client:
            client.post(base + "/v1/browser/activity", json=body,
                        headers={"Authorization": f"Bearer {bearer}"})
    except Exception:  # health: allow BLE001 -- best-effort activity must never change the browser answer
        logging.getLogger(__name__).warning("Browser activity could not be published")


def browse(args, task_id=None, **kwargs):
    from agent.redact import redact_sensitive_text
    from agent.secret_scope import get_secret
    from tools import browser_use_cli as engine
    from tools.browser_tool_session import run_fenced

    try:
        if set(args) - set(SCHEMA["parameters"]["properties"]):
            raise ValueError("Unknown browse parameter")
        url = args["url"]
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("browse needs an https URL without credentials")
        for key in ("goal", "expect", "allow_hosts", "target"):
            if not isinstance(args.get(key, ""), str):
                raise ValueError(f"{key} must be a string")
        target = args.get("target", "")
        if target and not re.fullmatch(r"[0-9A-F]{32}", target):
            raise ValueError("Invalid browser target")
        offset = args.get("text_offset", 0)
        if type(offset) is not int or not 0 <= offset <= 2000000:
            raise ValueError("text_offset must be between 0 and 2000000")
        if type(args.get("new_window", False)) is not bool:
            raise ValueError("new_window must be true or false")
        verdict = engine._code_url_verdict(json.dumps(url))
        if verdict.blocked:
            if "private or internal" in verdict.blocked and _unresolvable(parsed.hostname):
                raise ValueError(f"Could not look up {parsed.hostname} (DNS failed). Try again in a moment.")
            raise ValueError(verdict.blocked)
        env = engine._base_subprocess_env()
        error = engine._route_backend(env, "", task_id, False, force_local_sidecar=verdict.local_sidecar)
        if error:
            raise ValueError(error)
        if not engine._has_cdp_env(env):
            raise ValueError("browse requires Hermes's CDP browser; browser autospawn is not supported")
        if verdict.local_sidecar:
            env["BU_NAME"] = engine._sidecar_daemon_name("")
        local = bool(env.pop(engine._BOT_DESKTOP_BROWSER_SENTINEL, None))
        env.pop(engine._PRIVATE_BROWSER_SENTINEL, None)
        # Credentials travel only over the worker's stdin, never argv or the harness daemon's env.
        credentials = {key: get_secret(key) or "" for key in (
            "WEAVE_API_URL", "WEAVE_API_MCP_BEARER", "TYPESAFE_MODEL", "TEXT_MODEL",
        )}
        # The active chat's session comes from the native run context, never config or model arguments.
        from agent.vault_backends.weave import _run_context
        session = _run_context()["session_id"]
        credentials["WEAVE_HERMES_SESSION_ID"] = session
        # This chat's own Jev window carries on; idle Jev windows close; the person's windows are never named.
        now = time.time()
        reuse, stale = "", []
        with contextlib.suppress(Exception):
            reuse, stale = _plan_windows(session, now, _idle_seconds())
        worker_args = {key: value for key, value in args.items() if key != "new_window"}
        if not target and not args.get("new_window") and reuse:
            worker_args["reuse"] = reuse
        if stale:
            worker_args["close"] = stale
        payload = json.dumps({"args": worker_args, "credentials": credentials})
        for key in ("WEAVE_API_MCP_BEARER", "TYPESAFE_API_KEY", "TEXT_MODEL_API_KEY"):
            env.pop(key, None)

        def dispatch():
            engine._attach_vault_supervisor(env, task_id)
            with engine._driven_daemons_lock:
                engine._driven_daemons.add(env.get("BU_NAME", "default"))
            proc = engine._run_cli_killing_process_group(
                [sys.executable, str(Path(__file__).with_name("runner.py"))], payload, env, 300,
            )
            if proc.returncode:
                raise RuntimeError("Jev worker failed; inspect the current page before repeating an action")
            return json.loads(proc.stdout)

        result = run_fenced({"features": {"local": True}}, dispatch) if local else dispatch()
        announced = result.pop("_announced", [])
        if (result.get("browser_page") or {}).get("target") not in announced:
            _publish_activity(result, credentials)  # a resumed window: the worker opened nothing new
        owned = (result.get("browser_page") or {}).get("target")
        if isinstance(owned, str) and re.fullmatch(r"[0-9A-F]{32}", owned):
            with contextlib.suppress(Exception):
                _remember_window(session, owned, time.time())
    except subprocess.TimeoutExpired:
        result = {"success": False, "error_type": "timeout", "error":
                  "Jev exceeded 300 seconds. Inspect the current page before repeating any action."}
    except Exception as exc:  # health: allow BLE001 -- tool boundary returns JSON; forced redaction protects exception text
        result = {"success": False, "error_type": type(exc).__name__, "error": str(exc)}
    return redact_sensitive_text(json.dumps(result), force=True)


def _unresolvable(host):
    import socket
    try:
        socket.getaddrinfo(host, 443)
        return False
    except socket.gaierror:
        return True


def register(ctx):
    ctx.register_tool(name="browse", toolset="harso-jev", schema=SCHEMA, handler=browse)
