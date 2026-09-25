"""Fork shim for the Bot Screen port (tools/bot_desktop/UPSTREAM.md, diff 4).

Upstream split ``tools/browser_tool.py`` into modules; ``tools.bot_desktop.browser`` imports the Chromium
search roots from here. The fork keeps them in ``tools.browser_tool``, so this module only re-exports.
"""

from tools.browser_tool import _chromium_search_roots

__all__ = ["_chromium_search_roots"]
