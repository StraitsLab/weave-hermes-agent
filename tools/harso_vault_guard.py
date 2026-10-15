"""Harso vault document custody; upstream owns secret storage and fill construction."""
from __future__ import annotations

import json
import threading

_LOCK = threading.Lock()
# (task, endpoint, main-frame id) -> [current loader, filled loaders]. Keep endpoints private.
_TABS: dict[tuple[str, str, str], list] = {}


def armed(task_id=None):
    bare = task_id and task_id.removesuffix("::local")
    with _LOCK:
        return any(current in filled for (task, _, _), (current, filled) in _TABS.items()
                   if bare is None or task.removesuffix("::local") == bare)


def _shared(task_id):
    from tools import browser_tool as bt
    import os
    from hermes_cli.config import read_raw_config

    bare = (task_id or "default").removesuffix("::local")
    with bt._cleanup_lock:
        cached = [(bt._active_sessions.get(key) or {}).get("features") or {} for key in (bare, bare + "::local")]
    config = read_raw_config().get("browser") or {}
    return bool(os.environ.get("BROWSER_CDP_URL", "").strip() or config.get("cdp_url")
                or config.get("use_real_profile")
                or any(f.get("cdp_override") or f.get("real_profile") for f in cached))


def protect(*, tool_name, args, next_call, task_id=None, **context):
    """Execution middleware can refuse; upstream request middleware only rewrites arguments."""
    dangerous = tool_name in {"browser_exec", "browser_vision", "browser_cdp"} or (
        tool_name == "browser_console" and args.get("expression") is not None)
    if not dangerous:
        return next_call(args)
    try:
        shared = _shared(task_id)
    except Exception:  # health: allow BLE001 -- unknown browser reachability must fail closed
        shared = True
    if not armed(None if shared else task_id or "default"):
        return next_call(args)
    if tool_name == "browser_cdp":
        from tools.browser_cdp_tool import _CDP_PRIVATE_PAGE_ALLOWED_METHODS
        url = str((args.get("params") or {}).get("url") or "http:").lower()
        if args.get("method") in _CDP_PRIVATE_PAGE_ALLOWED_METHODS and url.startswith(("http:", "https:")):
            return next_call(args)
    return json.dumps({"success": False, "error_type": "vault_armed", "error":
                       "A tab holds a vault-filled value. JavaScript, browser code, raw CDP and screenshots are "
                       "blocked until it loads a new document. Click, press, snapshot and navigate remain available."})


def arm(supervisor, timeout=5.0):
    from hermes_cli.plugins import _delivery_manager
    from tools.browser_supervisor import _schedule

    if protect not in _delivery_manager()._middleware.get("tool_execution", []):
        raise RuntimeError("Enable the harso-vault-guard plugin before filling native vault values")
    with supervisor._state_lock:
        session = supervisor._page_session_id if supervisor._active else None
    if not session or supervisor._loop is None:
        raise RuntimeError("supervisor has no attached page session")
    response = _schedule(supervisor._cdp("Page.getFrameTree", session_id=session, timeout=timeout),
                         supervisor._loop, timeout=timeout + 1)
    frame = response["result"]["frameTree"]["frame"]
    key = (supervisor.task_id, supervisor.cdp_url, frame["id"])
    loader = frame["loaderId"]
    if not loader:
        raise RuntimeError("supervisor has no document loader")
    with _LOCK:
        record = _TABS.setdefault(key, [loader, set()])
        record[0] = loader
        record[1].add(loader)


def observe_frame(supervisor, params):
    frame = params.get("frame") or {}
    key = (supervisor.task_id, supervisor.cdp_url, frame.get("id"))
    with _LOCK:
        if key in _TABS:
            # A missing loader is unknown, not evidence that the secret-bearing document left.
            if frame.get("loaderId"):
                _TABS[key][0] = frame["loaderId"]


class VaultGuardMixin:
    def arm_vault(self, timeout=5.0):
        arm(self, timeout)

    def _on_vault_frame_navigated(self, params, session_id):
        from tools.browser_supervisor_frames import FrameTrackingMixin
        FrameTrackingMixin._on_frame_navigated(self, params, session_id)
        observe_frame(self, params)
