# P0-TOOL-APPEND rework round 4 (fix base 5bcf324b22ff6cfa285d96317eea72b529762c40)

Authority: .lane/ruling-r4.md. Finding: reviewer r3 R3.1 (quoted wrap-up notice deleted from a
legacy steer). Changed: agent/tool_row_append.py (`_peel_string_by_markers`),
tests/agent/test_durable_tool_append_r3.py, .lane/deviations.md (new), this file.

## Fix

`_peel_string_by_markers` (record-less rows only) no longer strips `\n\n` + RUN_BUDGET_WRAPUP_NOTICE
from a candidate piece before the steer-block test. The region is split only where a piece is EXACTLY
a steer block, so a notice between a CLOSE and a later OPEN stays inside its piece and the cut never
advances past its bytes. Recorded rows (`split_trailing_appends` record branch, list branch) are
untouched. The trailing-notice strip at the very end of the row is kept: it only removes notices after
the row's last CLOSE; every steer block ends with CLOSE and the notice contains no CLOSE, so those
bytes cannot be quoted steer text (asserted in `test_trailing_legacy_notice_still_dropped`).

Rewritten guard + accepted cost: `test_successive_legacy_appends_with_notice` now asserts the notice
is KEPT (D2 in .lane/deviations.md).

## New tests (TestR31LegacyNoticeKept, 11 cases)

| Variant | Test |
|---|---|
| split keeps every byte, 1/2/3 quoted notices | test_quoted_notice_split_keeps_every_byte[1,2,3] |
| prune fires, steer kept verbatim, re-prune stable | test_quoted_notice_survives_prune_and_reprune[1,2] |
| summary + single-exchange serializers keep both notices | test_quoted_notice_reaches_serializers[summary,single-exchange] |
| earlier steer still splits, no byte lost | test_quoted_notice_after_earlier_steer |
| DESIGN GUARD: trailing notice after last CLOSE dropped | test_trailing_legacy_notice_still_dropped |
| DESIGN GUARD: recorded notice between recorded steers consumed (string, list) | test_recorded_notice_between_steers_consumed[string,list] |

## Raw lines (throwaway worktree at HEAD, lane .venv symlinked, module path verified under the tree)

- Lane: r3 + durable tests `51 passed in 7.82s`.
- Reviewer r3 notice-loss probe, unchanged (cmp vs archive): `8 passed in 0.61s` (8 failed at 5bcf324b per review-gate-r3).
- Reviewer r3 hunt probe, unchanged: `8 passed in 3.53s`.
- r2 probes unchanged: r2_boundaries `7 passed`, r2_abort `1 passed`, probes `8 passed`, guards `7 passed`,
  bytes (REVIEW_SNAPSHOT set) `1 passed`, snapshot sha256 `d79ed7c3b4df3cab55a57e108b446dbd9169252ea510f627078b81f9f93e72fd` (matches).
- Mutants (selection: r3 + durable + classes + r3 probes + r2 boundaries/abort + probes + guards). Control `122 passed in 19.80s`.
  - (a) legacy notice stripped again (`region.replace(_NOTICE_PIECE, "")`): `19 failed, 103 passed`. KILLED
    (test_successive_legacy_appends_with_notice, test_quoted_notice_split_keeps_every_byte[1-3], reviewer
    notice_loss 8/8, test_review_r3_hunt::test_quoted_notice_between_markers_keeps_user_bytes[False-string], ...).
  - (b) cut advanced past the notice (the r3 strip-then-split loop restored): `19 failed, 103 passed`. KILLED
    (same named tests).
- Scoped suite: the 141-file r3 selection (138 set + r3 tests + r2 probe + bytes probe) + both r3 probes, HERMES_TEST_WORKERS=2, run once:
  `=== Summary: 143 files, 1814 tests passed, 1 failed, 2 skipped (100% complete) in 117.2s (2 workers) ===`.
  Only failure: `test_review_bytes.py` `KeyError: 'REVIEW_SNAPSHOT'` (runner drops the env var; same as r3), passes
  standalone with the sha above. 1814 = 1787 (r3) + 11 (r4 tests) + 16 (r3 probes).
- Strict P0 xfail marker untouched. Reviewer probes not committed. Logs: /Users/abbhinnav/.hermes/cache/scratch/p0r4/.
