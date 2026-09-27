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
