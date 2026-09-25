"""Fork shim for the Bot Screen port (tools/bot_desktop/UPSTREAM.md, diff 3).

Upstream split ``tools/browser_tool.py`` into modules and ``tools.bot_desktop`` imports the Chromium sandbox
policy from this one. The fork keeps that policy in ``tools.browser_tool``; this module re-exports it so the
dock's Browser icon and agent-browser read ONE list. It also carries copies of upstream's lease fence and
daemon-idle helper (``browser_tool_session.py:166-208,656-700`` @ ee5ee84a), trimmed of the cloud/lifecycle
module indirection the fork does not have.
"""

from typing import Any, Callable, Dict, Optional

from tools.browser_tool import (
    CHROMIUM_SANDBOX_BYPASS_ARGS,
    _needs_chromium_sandbox_bypass,
    apparmor_restricts_unprivileged_userns,
)

__all__ = ["CHROMIUM_SANDBOX_BYPASS_ARGS", "_needs_chromium_sandbox_bypass", "apparmor_restricts_unprivileged_userns"]

_SHARED_HEADED_DAEMON_IDLE_SECONDS = 24 * 3600


def daemon_idle_timeout_seconds() -> int:
    """The agent-browser daemon's self-termination timer. The headed Chromium on the Bot Screen is shared with a
    human who may take the lease to log in: the agent is idle by definition then, and the daemon cannot see the
    lease, so while a screen is published the lease-aware janitor owns that browser's lifetime."""
    from tools import browser_tool as _bt
    if _bt._is_headed_mode():
        from tools.bot_desktop.runtime import published_env
        if published_env().get("DISPLAY"):
            return _SHARED_HEADED_DAEMON_IDLE_SECONDS
    return _bt.BROWSER_SESSION_INACTIVITY_TIMEOUT


def human_holds_shared_browser(session_info: Dict[str, Any]) -> bool:
    """True while a human holds the lease over the browser ``session_info`` shares with them (janitor: activity)."""
    if not _shares_bot_desktop_browser(session_info):
        return False
    from tools.bot_desktop import lease as _bd_lease
    return _bd_lease.human_holds()


def ensure_screen_for_headed_chromium() -> None:
    """Tool-call boundary for ``bot_desktop.auto_start``: a headed Chromium is about to be spawned, so the screen
    comes up first. Headless browsing, Lightpanda and the env-only callers of ``_build_browser_env`` never start it."""
    from tools import browser_tool as _bt
    if _bt._is_headed_mode():
        from tools.bot_desktop.runtime import ensure_started_for_tool
        ensure_started_for_tool()


_HUMAN_HAS_CONTROL = ("A human has taken over this desktop (they may be entering a credential). Screen actions and "
                      "captures are refused until they hand control back. Tell the user what you need in your reply.")
_VOIDED = ("A human took over the bot's screen while this browser command ran; its result was discarded. Tell the "
           "user what you need; retry once they hand back.")


def run_fenced(session_key: str, act: Callable[[Dict[str, Any]], Dict[str, Any]],
               resolve: Optional[Callable[[], Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Resolve the ``session_key`` browser and ``act`` on it under the Bot Screen lease fence when that browser is
    the bot's LOCAL one (the page may show a human's credential). Cloud / user-supplied CDP sessions run unfenced.

    The epoch is taken FIRST and kept to the end, so every step is inside it:
    - before ``resolve`` (default: the cached session) — a cached shared browser under a human lease is refused
      before resolution could recycle or tear it down;
    - after ``resolve`` — refused before any browser I/O if a human holds it now;
    - after ``act`` — sharing is decided again, because a cold ``act`` may have started the screen itself; a lease
      that moved at any point since admission voids the result."""
    from tools.bot_desktop import lease as _bd_lease
    admitted = _bd_lease.get()
    if admitted.holder == _bd_lease.HUMAN and _shares_bot_desktop_browser(_cached_session(session_key)):
        return {"success": False, "error": _HUMAN_HAS_CONTROL, "code": "human_has_control"}
    session_info = resolve() if resolve else _cached_session(session_key)
    if _shares_bot_desktop_browser(session_info) and _lease_moved(admitted):
        return {"success": False, "error": _HUMAN_HAS_CONTROL, "code": "human_has_control"}
    result = act(session_info)
    if not (_shares_bot_desktop_browser(session_info) or _shares_bot_desktop_browser(_cached_session(session_key))):
        return result
    if _lease_moved(admitted):
        return {"success": False, "code": "human_has_control", "error": _VOIDED}
    return result


def _lease_moved(admitted: Any) -> bool:
    """A human holds the lease now, or held it at some point since ``admitted`` was read."""
    from tools.bot_desktop import lease as _bd_lease
    now = _bd_lease.get()
    return now.holder == _bd_lease.HUMAN or now.epoch != admitted.epoch


def _cached_session(session_key: str) -> Dict[str, Any]:
    from tools.browser_tool import _active_sessions, _cleanup_lock
    with _cleanup_lock:
        return _active_sessions.get(session_key) or {}


def _shares_bot_desktop_browser(session_info: Dict[str, Any]) -> bool:
    """Decided by provenance, not transport: every LOCAL session is a browser Hermes launched with this profile's
    Bot Screen DISPLAY. A human lease with the screen already gone still fences."""
    if not (session_info.get("features") or {}).get("local"):
        return False
    from tools.bot_desktop import lease as _bd_lease, runtime as _bd_runtime
    return bool(_bd_runtime.published_env().get("DISPLAY")) or _bd_lease.human_holds()
