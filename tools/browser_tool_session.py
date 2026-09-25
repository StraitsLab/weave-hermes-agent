"""Fork shim for the Bot Screen port (tools/bot_desktop/UPSTREAM.md, diff 3).

Upstream split ``tools/browser_tool.py`` into modules and ``tools.bot_desktop`` imports the Chromium sandbox
policy from this one. The fork keeps that policy in ``tools.browser_tool``; this module re-exports it so the
dock's Browser icon and agent-browser read ONE list. It also carries copies of upstream's lease fence and
daemon-idle helper (``browser_tool_session.py:166-208,656-700`` @ ee5ee84a), trimmed of the cloud/lifecycle
module indirection the fork does not have.
"""

from typing import Any, Callable, Dict

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


def run_fenced(session_info: Dict[str, Any], fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    """Run ``fn`` under the Bot Screen lease fence when ``session_info`` is the bot's LOCAL browser.

    While a human holds the lease every action AND read against that browser is refused (the page may show their
    credential); a takeover while ``fn`` ran voids its result. Cloud / user-supplied CDP sessions run unfenced."""
    if not _shares_bot_desktop_browser(session_info):
        return fn()
    from tools.bot_desktop import lease as _bd_lease
    try:
        admitted = _bd_lease.assert_agent_may_act()
    except _bd_lease.HumanHasControl as e:
        return {"success": False, "error": str(e), "code": "human_has_control"}
    result = fn()
    if _bd_lease.get().epoch != admitted.epoch:
        return {"success": False, "code": "human_has_control",
                "error": "A human took over the bot's screen while this browser command ran; its result was "
                         "discarded. Tell the user what you need; retry once they hand back."}
    return result


def _shares_bot_desktop_browser(session_info: Dict[str, Any]) -> bool:
    """Decided by provenance, not transport: every LOCAL session is a browser Hermes launched with this profile's
    Bot Screen DISPLAY. A human lease with the screen already gone still fences."""
    if not (session_info.get("features") or {}).get("local"):
        return False
    from tools.bot_desktop import lease as _bd_lease, runtime as _bd_runtime
    return bool(_bd_runtime.published_env().get("DISPLAY")) or _bd_lease.human_holds()
