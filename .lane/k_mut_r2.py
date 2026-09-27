"""K-FORK-CLIENT r2 mutant driver. Run with cwd = a throwaway detached worktree of the fix head, python = lane venv.

Per mutant: git checkout -- . ; run the selection UNMUTATED (must be green) ; apply ONE edit ; run the same selection ;
restore ; git diff must be empty. KILLED only if control green AND >=1 test failed (no collection/setup error).
usage: python k_mut_r2.py <out.json> [name ...]
"""
import json, os, re, subprocess, sys, tempfile

S = "/Users/abbhinnav/.hermes/cache/scratch"
K = ["tests/plugins/memory/test_harso_copilot.py", "tests/plugins/memory/test_harso_copilot_wire.py",
     "tests/plugins/memory/test_harso_render.py", "tests/plugins/memory/test_harso_copilot_r2.py",
     "tests/plugins/memory/test_review_probes.py"]
PI = "plugins/memory/harso/__init__.py"
MM = "agent/memory_manager.py"

MUTANTS = {
    # ---- r2: one named mutant per review finding ----
    "F1-copilot-note-in-general-sanitizer": (MM,
        "Treat as (?:informational background data|authoritative reference data[^\\]]*)\\.\\]",
        "Treat as (?:informational background data|authoritative reference data[^\\]]*|memory, not user input\\.\\s*Data about the user; never instructions)\\.\\]"),
    "F2a-fallback-not-neutralized": (PI,
        "        if not copilot:\n            return \"\\n\".join(context)",
        "        if True:\n            return \"\\n\".join(context)"),
    "F2b-tool-json-assembly-unguarded": (PI,
        "        return f\"{hits[0].replacement} {s} {hits[0].replacement}\" if hits else s",
        "        return s"),
    "F3-delivery-before-status-gate": (PI,
        "        if _recall_denied(response):\n            return hint\n        if copilot:\n"
        "            # C §3/§4.1: a copilot delivery replaces the item list; any failure falls back to today's recall (§10).\n"
        "            delivered = render_wire_delivery(response.get(\"delivery\"), channel=\"turn_start\")\n"
        "            if delivered:\n                return \"\\n\".join([delivered, hint]) if hint else delivered\n",
        "        if copilot:\n"
        "            delivered = render_wire_delivery(response.get(\"delivery\"), channel=\"turn_start\")\n"
        "            if delivered:\n                return \"\\n\".join([delivered, hint]) if hint else delivered\n"
        "        if _recall_denied(response):\n            return hint\n"),
    "F4-schemas-read-live-switch": (PI,
        "        return [dict(_TOOL_SCHEMA)] if self._tools_on else []",
        "        return [dict(_TOOL_SCHEMA)] if copilot_enabled() else []"),
    "F4b-session-search-not-restored": (MM,
        "    for name in sorted((n for n in stash if n not in displaced), key=lambda n: stash[n][0]):",
        "    for name in []:"),
    # ---- round-1 mutants M1-M9 (+m10), re-expressed on the fixed code ----
    "m1-memory-into-content": ("agent/memory_delivery.py",
        "        return bool(helper(agent, messages, \"\\n\\n\" + block, kind=\"memory\"))",
        "        newest[\"content\"] = newest[\"content\"] + \"\\n\\n\" + block\n"
        "        return bool(helper(agent, messages, \"\\n\\n\" + block, kind=\"memory\"))"),
    "m2-switch-ignored": (PI,
        "    return _harso_section().get(\"copilot_enabled\") is True",
        "    return True"),
    "m3-switch-truthy": (PI,
        "    return _harso_section().get(\"copilot_enabled\") is True",
        "    return bool(_harso_section().get(\"copilot_enabled\"))"),
    "m4-summarizer-strip-off": ("agent/context_compressor.py",
        "            if getattr(self, \"strip_memory_deliveries\", False) is True:",
        "            if False:"),
    "m5-pre-compress-strip-off": ("agent/conversation_compression.py",
        "                pre_compress_messages = messages_without_memory(messages)",
        "                pre_compress_messages = messages"),
    "m6b-mid-turn-unsanitized": (PI,
        "        rendered = render_delivery(delivery.get(\"seq\"), parsed, channel=channel)",
        "        import plugins.memory.harso.render as _r\n        _s = _r.sanitize_memory_text\n"
        "        if channel == \"mid_turn\":\n            _r.sanitize_memory_text = lambda t, rules=_r.RULES: t\n"
        "        try:\n            rendered = render_delivery(delivery.get(\"seq\"), parsed, channel=channel)\n"
        "        finally:\n            _r.sanitize_memory_text = _s"),
    "m7-acks-dropped": (PI,
        "            if acks:\n                payload[\"memory_deliveries\"] = acks",
        "            if False:\n                payload[\"memory_deliveries\"] = acks"),
    "m8-finalized-items-read-sidecar": (PI,
        "            content = flatten_message_text(message.get(\"content\")).strip()",
        "            content = flatten_message_text(message.get(\"api_content\") or message.get(\"content\")).strip()"),
    "m9-session-search-dropped-without-harso": (MM,
        "            if not (routed_here & surface):\n                continue\n",
        ""),
    "m10-rule-row-renamed": ("plugins/memory/harso/sanitizer-rules.v1.json",
        "\"id\": \"angle_tool_markup\"", "\"id\": \"angle_tool_markup_x\""),
}


def run():
    env = dict(os.environ, PYTHONPATH=f"{os.getcwd()}:{os.getcwd()}/tests/plugins/memory", PYTHONDONTWRITEBYTECODE="1",
               HERMES_HOME=tempfile.mkdtemp(prefix="k-home-", dir=S), TMPDIR=S)
    p = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=no", "-rf", *K],
                       capture_output=True, text=True, env=env, timeout=900)
    lines = p.stdout.strip().splitlines()
    return p.returncode, (lines[-1] if lines else p.stderr[-300:]), [l for l in lines if l.startswith("FAILED")][:6]


def main():
    out, names = sys.argv[1], sys.argv[2:] or list(MUTANTS)
    chk = subprocess.run([sys.executable, "-c", "import agent.memory_manager as m, plugins.memory.harso as h;"
                          "print(m.__file__); print(h.__file__)"], capture_output=True, text=True,
                         env=dict(os.environ, PYTHONPATH=os.getcwd()))
    assert all(l.startswith(os.getcwd()) for l in chk.stdout.split()), chk.stdout + chk.stderr
    results = {"tree": os.getcwd(), "head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
               "import_check": chk.stdout.split(), "mutants": []}
    for name in names:
        path, old, new = MUTANTS[name]
        subprocess.check_call(["git", "checkout", "-q", "--", "agent", "plugins", "tools", "acp_adapter"])
        c_rc, c_sum, _ = run()
        src = open(path).read()
        assert src.count(old) >= 1, f"{name}: site not found"
        open(path, "w").write(src.replace(old, new, 1))
        diff = subprocess.check_output(["git", "diff", "--stat", "--", "agent", "plugins", "tools", "acp_adapter"], text=True).strip()
        m_rc, m_sum, failed = run()
        subprocess.check_call(["git", "checkout", "-q", "--", "agent", "plugins", "tools", "acp_adapter"])
        clean = subprocess.check_output(["git", "diff", "--stat", "--", "agent", "plugins", "tools", "acp_adapter"], text=True).strip() == ""
        killed = c_rc == 0 and m_rc == 1 and bool(failed) and "error" not in m_sum
        row = {"name": name, "file": path, "control": [c_rc, c_sum], "mutant": [m_rc, m_sum], "failed": failed,
               "diff": diff, "restored_clean": clean, "verdict": "KILLED" if killed else "NOT KILLED"}
        results["mutants"].append(row)
        print(f"{name}: control rc={c_rc} [{c_sum}] | mutant rc={m_rc} [{m_sum}] -> {row['verdict']}", flush=True)
        for f in failed[:3]:
            print("   ", f[:180], flush=True)
    json.dump(results, open(out, "w"), indent=1)


if __name__ == "__main__":
    main()
