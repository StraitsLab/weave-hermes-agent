"""K-FORK-CLIENT r3 mutants (F4 content gate). Reuses the r2 driver (control green first, KILLED only if >=1 named
test FAILS, tree restored after each). cwd = throwaway detached worktree of the r3 head; python = lane venv.
usage: python k_mut_r3.py <out.json>
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import k_mut_r2 as k  # noqa: E402

PI = k.PI
# Committed tests only: the reviewer probes are run separately and never counted as killers here.
k.K = [t for t in k.K if "review_probes" not in t]
k.MUTANTS = {
    "F4a-tool-pre-post-gate-removed": (PI,
        "        if _copilot_off():\n"
        "            # Still routed (the latch), but the live switch is OFF: a content-free result and zero requests.\n"
        "            return _COPILOT_OFF_RESULT\n",
        ""),
    "F4b-tool-post-fetch-recheck-removed": (PI,
        "        if _copilot_off():\n            return _COPILOT_OFF_RESULT\n        if response is None",
        "        if response is None"),
    "F4c-late-fetch-recheck-removed": (PI,
        "        if _copilot_off() or not response or _recall_denied(response):",
        "        if not response or _recall_denied(response):"),
    "F4d-turn-start-recheck-removed": (PI,
        "        if copilot and _copilot_off():",
        "        if False:"),
}

if __name__ == "__main__":
    k.main()
