# Preflight: weave-hermes-agent #69 (MEM-FACT-HISTORY recall rendering), review r1 rework

Replaces the stale #61 preflight (review r1 F8). Paired with weave-cloud #1267; the full paired matrix, mutant
table and cloud gates are in weave-cloud `.lane/preflight.md` on `mem/fact-history`.

## Identities
- Base `627d6829a` (codex/wev-repin-v0.21.0). Reviewed head `d0c7689d4` (BLOCK r1, gpt-6.1-sol, cross-family).
- Cloud side: #1267 merged to main at `eb13a8277` before the rework; the rework is on cloud `mem/fact-history`.

## Acceptance (fork scope: read-only rendering of cloud-provided history fields)
| Rule | Test |
|---|---|
| A corrected fact renders when it changed and what it was | `test_corrected_fact_renders_when_it_changed_and_what_it_was` |
| Conditional values render together under their label | `test_conditional_values_render_together` |
| Invalid/absent history renders exactly as before | `test_without_valid_history_the_recall_renders_exactly_as_before` (8 cases) |
| F7B: an out-of-range timestamp degrades that time only, never the recall | `test_out_of_range_changed_at_renders_no_history_and_keeps_the_recall` (2), `test_out_of_range_previous_said_at_keeps_the_value_without_a_time` (2), `test_short_time_never_raises` (5) |

## Fix (F7B)
`_short_time` now parses, converts to UTC and formats inside one `try` and returns `""` on ValueError/OverflowError
(`0001-01-01T00:00:00+14:00` and `9999-12-31T23:59:59-12:00` overflowed in `astimezone`, outside the old try).

## Evidence
- Red at `d0c7689d4` (old plugin, new tests): 6 failed, 3 passed (4 render tests + the 2 extreme `_short_time` cases raise `OverflowError`; the 3 malformed-string cases already returned ""). Reviewer fork probe: 2 failed.
- Green: `scripts/run_tests.sh tests/plugins/memory/test_harso_provider.py -j 1`: 266 passed, 0 failed.
- Reviewer's `/tmp/rv1267/test_fork_probes.py` (paths rebound only): 6 passed.
- Mutant m5 (raw value on parse failure + newline allowed), on a copy: KILLED, 10 failed / 256 passed.
- Not run: the full fork suite (reviewer saw acp dependency failures unrelated to this file).
