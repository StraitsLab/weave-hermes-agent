"""Round-4 mutants (ruling-r4): run from a THROWAWAY worktree root; committed tests only, reviewer probes excluded.

Usage: python k_mut_r4.py <out.json>. Control must be green first; a mutant counts only if a named test FAILS.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

SEL = ["tests/plugins/memory/", "tests/agent/test_turn_context.py", "tests/agent/test_api_content_sidecar.py"]
MUTANTS = {
    "a_stamped_recheck_removed": ("agent/turn_context.py",
        "        ext_prefetch_cache = withhold_off_origin_memory(agent, ext_prefetch_cache)\n", ""),
    "b_unstamped_recheck_removed": ("agent/conversation_loop.py",
        "                        _ext_prefetch_cache = withhold_off_origin_memory(agent, _ext_prefetch_cache)\n", ""),
    "c_origin_from_delivery_marker_only": ("agent/turn_context.py",
        "            if origin() is not True:\n",
        "            if \"Harso memory delivery\" not in ext_prefetch_cache:\n"),
}


def run():
    env = dict(os.environ, HERMES_HOME=tempfile.mkdtemp(prefix="k-r4-mut-"), PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([".venv/bin/python", "-B", "-m", "pytest", *SEL, "-q", "-p", "no:cacheprovider", "-rf"],
                       capture_output=True, text=True, env=env)
    out = p.stdout + p.stderr
    summary = [l for l in out.splitlines() if re.search(r"\d+ (passed|failed)", l)][-1:]
    failed = sorted(set(re.findall(r"^FAILED (\S+)", out, re.M)))
    return {"rc": p.returncode, "summary": summary[0] if summary else "", "failed": failed}


def main(dest):
    assert not any(os.path.exists(f"tests/plugins/memory/{n}") for n in (
        "test_r3_cache_boundary.py", "test_r2_boundary_probes.py")), "reviewer probes must not be in the mutant tree"
    result = {"control": run()}
    print("control", result["control"]["rc"], result["control"]["summary"], flush=True)
    assert result["control"]["rc"] == 0, "control not green"
    for name, (path, old, new) in MUTANTS.items():
        src = open(path).read()
        assert src.count(old) == 1, f"{name}: anchor not unique"
        open(path, "w").write(src.replace(old, new))
        try:
            r = run()
        finally:
            open(path, "w").write(src)
        r["killed"] = r["rc"] == 1 and bool(r["failed"])
        result[name] = r
        print(name, "KILLED" if r["killed"] else "SURVIVED", r["summary"], *r["failed"], sep="\n  ", flush=True)
    json.dump(result, open(dest, "w"), indent=2)


if __name__ == "__main__":
    main(sys.argv[1])
