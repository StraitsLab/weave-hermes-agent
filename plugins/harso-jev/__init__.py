"""Bounded Jev goals on Hermes's own browser backend."""
import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA = {
    "name": "browse",
    "description": (
        "Open and read a page, or perform one narrow browser goal on Hermes's browser computer. "
        "Empty goal returns visible page text. expect is independently checked against the live page. "
        "allow_hosts adds comma-separated hosts (the starting host is always allowed). "
        "Pass browser_page.target to resume that exact tab WITHOUT navigating; text_offset reads more text. "
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
        },
        "required": ["url"],
        "additionalProperties": False,
    },
}


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
        verdict = engine._code_url_verdict(json.dumps(url))
        if verdict.blocked:
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
            "WEAVE_API_URL", "WEAVE_API_MCP_BEARER", "WEAVE_HERMES_SESSION_ID",
            "TYPESAFE_MODEL", "TEXT_MODEL",
        )}
        payload = json.dumps({"args": args, "credentials": credentials})
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
        _publish_activity(result, credentials)
    except subprocess.TimeoutExpired:
        result = {"success": False, "error_type": "timeout", "error":
                  "Jev exceeded 300 seconds. Inspect the current page before repeating any action."}
    except Exception as exc:  # health: allow BLE001 -- tool boundary returns JSON; forced redaction protects exception text
        result = {"success": False, "error_type": type(exc).__name__, "error": str(exc)}
    return redact_sensitive_text(json.dumps(result), force=True)


def register(ctx):
    ctx.register_tool(name="browse", toolset="harso-jev", schema=SCHEMA, handler=browse)
