#!/bin/bash
# usage: k_mut.sh <name> <file> <python-replace-old> <python-replace-new>
S=/Users/abbhinnav/.hermes/cache/scratch; M=$S/k-mut; cd $M; git checkout -q -- .
python3 - "$2" "$3" "$4" <<'PY'
import sys; p,o,n=sys.argv[1:4]; s=open(p).read(); assert s.count(o)>=1, "mutant site not found"; open(p,'w').write(s.replace(o,n,1))
PY
out=$(timeout 600 .venv/bin/python -m pytest -q -p no:cacheprovider tests/plugins/memory/test_harso_copilot.py tests/plugins/memory/test_harso_copilot_wire.py tests/plugins/memory/test_harso_render.py 2>&1 | tail -1)
echo "MUTANT $1: $out"
timeout 600 .venv/bin/python -m pytest -q -p no:cacheprovider tests/plugins/memory/test_harso_copilot.py tests/plugins/memory/test_harso_copilot_wire.py tests/plugins/memory/test_harso_render.py 2>&1 | grep '^FAILED' | head -6
git checkout -q -- .
