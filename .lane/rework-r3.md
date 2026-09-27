# K-FORK-CLIENT round 3 — F4 content gate (per .lane/ruling-r3.md)

Fix commit: dff21e9d60 (parent ffb4e33230). F1-F3 untouched. Files: plugins/memory/harso/__init__.py,
tests/plugins/memory/test_harso_copilot_r2.py.

## Change
- One helper `_copilot_off()` (= `not copilot_enabled()`); no second config or routing authority.
- Tool (`handle_tool_call`): `_tools_on` latch unchanged (routing). After argument validation and BEFORE `_post`:
  live OFF -> `{"error": "Harso memory is off"}`, zero requests. After `_post` returns: re-check, same result.
- Late fetch (`fetch_mid_turn_delivery`): re-check after `_post`, before status gate/render -> "".
- Turn-start (`prefetch`): only when the request was sent copilot-ON (`copilot` sampled before `_post`) and the switch
  is OFF when it returns: return the hint only (no delivery, no items). A turn that starts OFF never enters that branch,
  so the OFF path is unchanged bytes. Deviation note: the ruling says "continue exactly as the copilot-OFF path does
  today (legacy items + hint)". I withheld the legacy items too because the in-flight request was copilot-shaped
  (sent visible_seqs) and the reviewer's unchanged probe asserts `INFLIGHT_SENTINEL` (an item) is not rendered after
  OFF; rendering items would fail that probe. Start-OFF turns still render legacy items + hint exactly as base.
- Test: `test_f4_hot_disable_before_refresh_keeps_harso_memory_routable` now asserts routed dispatch returns the
  content-free result with ZERO requests (brief/search/open). Added post-fetch OFF tests for tool, late, turn-start,
  plus an ON positive control for all three.

## Proof (raw lines; logs in .lane/r3-logs/)
- Reviewer r2 probe, UNCHANGED (`cmp` vs probes-r2/test_r2_boundary_probes.py -> PROBE_UNCHANGED), throwaway worktree
  of dff21e9d60, reviewer's command: `13 passed in 1.43s`, EXIT 0 (boundary-probes-r3.log). OFF lines now read
  `posts=0 rendered=False`; `rendered_after_off=False` for tool/late/prefetch. (6 red at ffb4e332 per reviewer.)
- Round-1 reviewer probes unchanged (`cmp` ok): `19 passed in 1.37s` (review-probes-r1.log).
- probe_default_off.py output `cmp`-identical to reviewer's default-off-base.log (default-off-r3.log).
- Switch-off fixed-transcript probe (.lane/k_probe_switch_off.py): 7 requests, sha256 697069a5f863ad93… = base.
- Mutants (.lane/k_mut_r3.py over k_mut_r2 driver; throwaway worktree; committed tests only, reviewer probes
  excluded; control green first) -> .lane/k-mut-r3.json, mutants-r3.log:
  - F4a tool pre-post gate removed: control `754 passed, 1 xfailed` | mutant `3 failed` KILLED
    (test_f4_hot_disable_before_refresh_keeps_harso_memory_routable[brief|search|open])
  - F4b tool post-fetch re-check removed: `1 failed` KILLED (test_f4_tool_switch_off_during_fetch_returns_content_free_result)
  - F4c late-fetch re-check removed: `1 failed` KILLED (test_f4_late_fetch_switch_off_during_fetch_renders_nothing)
  - F4d turn-start re-check removed: `1 failed` KILLED (test_f4_turn_start_switch_off_during_fetch_withholds_memory)
  - A first mutant attempt selected the uncopied round-1 probe file: control rc=4, zero tests; NOT counted.
- Scoped set ONCE, sequential, no -n, r2 selection + both reviewer probe files (throwaway copies):
  `1679 passed, 2 skipped, 1 xfailed in 39.04s`, EXIT 0, 0 FAILED/ERROR lines (k-r3-scoped.log).
  Reconciles with r2's 1660: +13 r2 probes, +2 parametrized OFF cases, +4 new tests.
