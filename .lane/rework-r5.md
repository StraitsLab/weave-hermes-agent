# K-FORK-CLIENT round 5 (final): fence note frozen with the first-consumption gate, per .lane/ruling-r5.md

Scope was cut by the lead: fix the r4 finding only. Proof is the reviewer r4 probe RED -> GREEN, the r3/r2 probes still
green, and the touched test files. Mutants and the scoped-suite run were dropped. The lead runs the wide selection
(.lane/scoped-selection.txt) alongside re-review.

## Change
- `agent/turn_context.py`: new field `TurnContext.ext_prefetch_note` (:591). The stamped path decides
  `ext_prefetch_note = memory_fence_note(agent)` once, right after the r4 gate (:1606). It composes with that value (:1609)
  and carries it to the loop (:1693).
- `agent/conversation_loop.py`: the loop takes `_ext_prefetch_note` from the context (:2015). The unstamped first
  consumption decides the note once, together with the `withhold_off_origin_memory` gate (:2395). Every live composition
  now uses the frozen `_ext_prefetch_note` (:2401) and no longer calls `memory_fence_note(agent)` on each pass.
- Compression/rebuild fallback: this path has no separate composition site. If compaction drops the stamped sidecar
  (`drop_stale_api_content`), the loop falls through to the same live-compose block. `_ext_prefetch_gated` is already True
  and `_ext_prefetch_note` is the stamped path's frozen value, so the fallback reuses the same note. This is shown by
  source structure only; no separate compression test was run (mutants dropped by lead).
- Unchanged: the MoA reference suffix stays per-call (only the memory note is frozen). Stamped and historical sidecars,
  OFF-origin legacy behaviour (the note is None when the switch is off at the gate), and config reads (the existing
  `memory_fence_note(agent)` is called once) are all unchanged.

## RED / GREEN (raw; logs in .lane/r5-logs/)
The probes were copied unchanged, and the sha256 of each copy matches the .lane/reviewer-probes-r4 file
(probe-hashes.txt). r4 hunt 8608a911…
- At 1d89ba0a (throwaway detached worktree, reviewer's command): `2 failed, 6 passed in 9.08s`, EXIT=1
  (r4-hunt-at-1d89ba0a.log). FAILED `[True-delivery-True]` and `[True-items-True]`. The SHAs match the reviewer's
  79eebd79…/5e985df5… and 1e219cfc…/09ca8eed….
- At the fix: `8 passed in 7.80s`, EXIT=0. All 8 cases report `bytes_equal=True` (r4-hunt-at-fix.log).
- r3 cache boundary `8 passed`. Both `start_on=True end_on=False … first_sidecar_contains_memory=False plain_text_kept=True`.
- r3 hunt `10 passed`. r2 boundary `13 passed`.
- Touched modules (test_harso_copilot_r4, test_harso_copilot, tests/agent/test_turn_context,
  tests/agent/test_api_content_sidecar): `115 passed in 15.80s`, EXIT=0.

## Scoped selection for the lead
.lane/scoped-selection.txt lists r4's selection plus the reviewer probe copies at their tree paths. Before running it,
copy the probes into tests/plugins/memory/ unchanged:
- .lane/reviewer-probes-r4/{test_r2_boundary_probes,test_r3_cache_boundary,test_r3_hunt,test_r4_reviewer_hunt}.py
- .lane/reviewer-probes-r1/test_review_probes.py (the round-1 probe, sha256 2f712698…)

Collect-only: 1730 tests. That is r4's 1714 (1711 passed + 2 skipped + 1 xfailed), plus test_r3_hunt (10), plus
test_r4_reviewer_hunt (8). The count without those two files is 1712, not 1714; the 2-test difference was not
investigated.
