# P0-TOOL-APPEND deviations from original behaviour (accepted by lead ruling)

## D1 (r3, ruling-r3): legacy row upgraded by its first recorded append

Older unrecorded steer text on a row that later receives its first recorded append is treated as
tool output (kept in the transcript, prunable). Pinned by
`test_legacy_row_upgraded_by_first_recorded_append`. See .lane/rework-r3.md.

## D2 (r4, ruling-r4 item 2): notice between genuine successive legacy appends is KEPT

On a record-less (legacy) string row, a wrap-up notice that sits between a steer CLOSE and a later
steer OPEN is kept in place as protected text. A genuine `steer + notice + steer` sequence appended
before span records existed is byte-identical to one user steer quoting `CLOSE + notice + OPEN`, so
it can no longer be split into two steers with the notice dropped: the two steers and the notice
come back as one protected piece. Keeping a possible system notice is the accepted one-time cost;
deleting a possible user quotation is not allowed. Only rows appended before the fork upgrade are
affected (recorded rows consume a recorded notice exactly as before).

- Rewritten guard: `tests/agent/test_durable_tool_append_r3.py::TestR22LegacyKeepsMore::test_successive_legacy_appends_with_notice`
  now asserts the joined pieces equal the whole suffix and the notice appears once. It still
  asserts that successive legacy appends WITHOUT a notice split into `["A1", "B2"]`.
- Unchanged: a notice at the very end of a legacy row (after its last CLOSE) is still dropped.
  Every steer block ends with CLOSE and the notice contains no CLOSE, so those bytes cannot be
  steer text (`test_trailing_legacy_notice_still_dropped`).

## D3 (r5, ruling-r5): a record-less multi-block region is ATOMIC

On a record-less (legacy) string row, the protected region (earliest steer OPEN to the final
CLOSE) is returned as ONE piece and is never split at an inner CLOSE/OPEN pair. Only its
outermost producer OPEN and CLOSE are unwrapped (`steer_texts`); every inner byte, markers
included, stays verbatim for pruning, re-pruning, both serializers and the deterministic
fallback. Reason: two genuine successive legacy appends are byte-identical to one user steer
quoting `CLOSE + OPEN` (reviewer R4.1), and splitting them let the fallback delete user bytes.

- Accepted cost: genuine successive legacy appends (`A1`, `B2`) are no longer split; they
  come back as one piece whose text is `A1\n<CLOSE>\n\n<OPEN>\nB2`.
- Rewritten guards (tests/agent/test_durable_tool_append_r3.py):
  `TestR22LegacyKeepsMore::test_successive_legacy_appends_with_notice` (now asserts one piece,
  with and without the notice) and `TestR31LegacyNoticeKept::test_quoted_notice_after_earlier_steer`
  (now asserts one piece).
- Unchanged: recorded rows split per steer exactly; list-shaped legacy rows (one text block per
  append) unchanged; D2's notice rule and the trailing-notice drop unchanged.
