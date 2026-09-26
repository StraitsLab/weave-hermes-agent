"""Fork shim for the Bot Screen port (tools/bot_desktop/UPSTREAM.md, diff 3).

Upstream split ``tools/browser_tool.py`` into modules and ``tools.bot_desktop`` imports the Chromium sandbox
policy from this one. The fork keeps that policy in ``tools.browser_tool``; this module re-exports it so the
dock's Browser icon and agent-browser read ONE list.
"""

from tools.browser_tool import (
    CHROMIUM_SANDBOX_BYPASS_ARGS,
    _needs_chromium_sandbox_bypass,
    apparmor_restricts_unprivileged_userns,
)

__all__ = ["CHROMIUM_SANDBOX_BYPASS_ARGS", "_needs_chromium_sandbox_bypass", "apparmor_restricts_unprivileged_userns"]
