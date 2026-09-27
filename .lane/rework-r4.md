# K-FORK-CLIENT round 4 (final): F4-cache, per .lane/ruling-r4.md

Fix commit: 4044d6ffcb (parent a291a525f2). F1-F3 and the r3 F4 gates are untouched. Files: plugins/memory/harso/__init__.py,
agent/turn_context.py, agent/conversation_loop.py, tests/plugins/memory/test_harso_copilot_r4.py (new).
agent/memory_delivery.py is unchanged.

## Change
- Provider origin (ruling §1): `HarsoMemoryProvider.prefetch()` resets `_prefetch_origin_on` on entry and sets it from
  the `copilot_enabled()` sample that shapes the request. `prefetch_copilot_origin()` exposes it. The flag belongs to the
  fetch, not to the rendered text, so it covers structured delivery and the legacy-item fallback alike. Origin is never
  inferred from the delivery marker.
- One gate, `turn_context.withhold_off_origin_memory(agent, cache)` (ruling §2): for each manager provider that
  reports an ON origin, it re-reads that provider's own `copilot_active()`, which is the same live switch
  (`copilot_enabled()`) the provider reads. If the switch is now OFF, or either probe raises, it returns "" and the
  whole unsent memory cache is withheld. User text and `plugin_user_context` are composed separately and are kept.
  OFF-origin caches, and providers without the probe, pass through as the same object (OFF stays byte-identical to base).
- Stamped path: `build_turn_context` applies the gate right before `compose_user_api_content`, after
  prefetch/describe_recall, then sets `ext_prefetch_gated=True`. The gated cache is what goes into
  `TurnContext.ext_prefetch_cache`, so the loop cannot re-inject it.
- Unstamped path: `conversation_loop` live composition applies the gate once, at first consumption, when the prologue
  did not (`_ext_prefetch_gated` is False: MoA / no-stamp turns). Later passes reuse the result, so bytes within a
  turn stay stable.
- Unchanged: already-sent historical sidecars (replayed verbatim), tool advertisement and routing, and the OFF path.
  No new config read and no new routing authority.

## RED / GREEN (raw lines; logs in .lane/r4-logs/)
- Reviewer r3 probe UNCHANGED (`cmp` -> PROBES_UNCHANGED). Throwaway worktree of 4044d6ffcb with the lane .venv symlinked,
  reviewer's exact command:
  - at a291a525 (RED): `2 failed, 6 passed in 1.59s` (r3-probe-at-base.log)
  - at 4044d6ffcb (GREEN), run twice: `8 passed in 2.30s`, `8 passed in 1.29s`, EXIT 0 (r3-probe-run1/2.log)
    `start_on=True end_on=False representation=delivery requests=1 first_sidecar_contains_memory=False plain_text_kept=True`
    `start_on=True end_on=False representation=items requests=1 first_sidecar_contains_memory=False plain_text_kept=True`
    All six controls still print `first_sidecar_contains_memory=True`.
- Reviewer r2 boundary probe UNCHANGED: `13 passed in 1.65s`, EXIT 0 (r2-probe.log).
- New tests at base a291a525. The file fails to import there (`ImportError: cannot import name 'withhold_off_origin_memory'`,
  red-at-base.log). A behavioral variant that only drops that import:
  `11 failed, 6 passed, 7 deselected` (red-at-base-behavioral.log). The failures are stamped delivery/items True->False,
  all 8 unstamped cases, and the origin test. Unstamped controls also fail at base because the test asserts the
  `ctx.ext_prefetch_gated` flag, which is new.
- At 4044d6ffcb: `24 passed in 8.48s`.

## Mutants (.lane/k_mut_r4.py; throwaway worktree of 4044d6ffcb; committed tests only, reviewer probes asserted absent)
Selection: tests/plugins/memory/, tests/agent/test_turn_context.py, tests/agent/test_api_content_sidecar.py.
Control first: `1361 passed, 2 skipped, 1 xfailed` rc=0. Results in .lane/k-mut-r4.json and r4-logs/mutants-r4.log.

| mutant | result | named failing tests |
|---|---|---|
| (a) stamped-path re-check removed | `2 failed` KILLED | test_r4_stamped_first_composition_rechecks_live_switch[delivery-True-False], [items-True-False] |
| (b) unstamped-path re-check removed | `2 failed` KILLED | test_r4_unstamped_first_composition_rechecks_live_switch[delivery-True-False], [items-True-False] |
| (c) origin inferred from delivery marker only | `5 failed` KILLED | test_r4_stamped_…[items-True-False], test_r4_unstamped_…[items-True-False], test_r4_withhold_helper_contract ×3 |

## Scoped suite (ONE run, sequential, no -n)
r3 selection plus the reviewer probe copies (r2 boundary, review probes, r3 cache boundary) in the throwaway tree:
`1711 passed, 2 skipped, 1 xfailed in 64.02s`, EXIT 0, 0 FAILED/ERROR lines (k-r4-scoped.log).
This reconciles with r3's 1679: +24 new r4 tests and +8 r3 probe cases.
Touched-module neighbours (test_turn_context, test_api_content_sidecar, test_gateway_turn_sidecar,
test_turn_context_overflow_warning, test_identity_epoch_rebuild): `74 passed in 11.41s` (k-r4-touched.log).
The whole fork suite was not rerun.
