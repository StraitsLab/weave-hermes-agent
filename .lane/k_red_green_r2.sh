#!/bin/bash
# RED on the reviewed head's production bytes, GREEN on the fix. Never git stash: copies + git show + cmp.
# usage: .lane/k_red_green_r2.sh <log>
set -u
OLD=f238e3391f2f8db46edd978367b77a6a9699d18f
FILES="agent/memory_manager.py plugins/memory/harso/__init__.py tools/mcp_tool.py acp_adapter/server.py"
PROBES=/Volumes/MainData/AgentTools/codex/evidence/lane-archive/wave-1/K-FORK-CLIENT/probes
LOG=${1:-/Users/abbhinnav/.hermes/cache/scratch/k-r2-redgreen.log}
cd "$(git rev-parse --show-toplevel)"
SAVE=$(mktemp -d /Users/abbhinnav/.hermes/cache/scratch/k-r2-save-XXXX)
cp "$PROBES/test_review_probes.py" tests/plugins/memory/test_review_probes.py   # reviewer probe, UNCHANGED
cmp "$PROBES/test_review_probes.py" tests/plugins/memory/test_review_probes.py || exit 9
run() {
  HERMES_HOME=$(mktemp -d /Users/abbhinnav/.hermes/cache/scratch/k-home-XXXX) PYTHONPATH="$PWD:$PWD/tests/plugins/memory" \
    .venv/bin/python -B -m pytest -q -p no:cacheprovider -rA --tb=no \
    tests/plugins/memory/test_harso_copilot_r2.py tests/plugins/memory/test_review_probes.py 2>&1 \
    | grep -E '^(PASSED|FAILED|ERROR) |passed|failed'
  HERMES_HOME=$(mktemp -d /Users/abbhinnav/.hermes/cache/scratch/k-home-XXXX) TMPDIR=/Users/abbhinnav/.hermes/cache/scratch \
    PYTHONPATH=$PWD .venv/bin/python "$PROBES/probe_default_off.py" | sed 's/^/DEFAULT_OFF_PROBE /'
}
{
  echo "== fix head $(git rev-parse HEAD) + working tree; production sha256:"; shasum -a 256 $FILES
  for f in $FILES; do mkdir -p "$SAVE/$(dirname $f)"; cp "$f" "$SAVE/$f"; git show "$OLD:$f" > "$f"; done
  echo "== RED: production files = $OLD"; shasum -a 256 $FILES
  run
  for f in $FILES; do cp "$SAVE/$f" "$f"; cmp "$SAVE/$f" "$f" || echo "RESTORE MISMATCH $f"; done
  echo "== GREEN: production files restored (cmp ok)"; shasum -a 256 $FILES
  run
} > "$LOG" 2>&1
rm -f tests/plugins/memory/test_review_probes.py
echo "log: $LOG"; grep -cE '^FAILED' "$LOG"; grep -E 'passed|failed' "$LOG" | grep -v '^PASSED\|^FAILED'
