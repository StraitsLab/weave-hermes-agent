# P0-TOOL-APPEND rework round 3 (fix base 9da323ecd2b439b0d8b901f33913a941fb1fd627)

Authority: .lane/ruling-r3.md. F1.a-F1.d and the dedup fix untouched (their tests all still green).
Changed: agent/tool_row_append.py, agent/context_compressor.py, tests/agent/test_durable_tool_append_r3.py (new).

## Class and fix

One class: which bytes of a tool row are user-origin, decided from marker text instead of provenance.

- R2.1 (records authoritative). `split_trailing_appends` now runs marker parsing ONLY when the row
  has no `tool_appends` records. With records: consume them from the end; if all are consumed, the
  rest is ordinary tool output, returned as is. If a record no longer matches, or the record list is
  malformed (a non-list value or a bad entry), nothing is guessed: the whole unmatched string tail, or
  the whole trailing text-block run for a list, stays protected. The string and list branches are
  symmetric.
- R2.2 (legacy keeps more). `_peel_string_by_markers` (record-less rows only): when the content ends
  with CLOSE, everything from the EARLIEST `\n\nOPEN\n` to the end is protected. A CLOSE between
  openings is no longer treated as a boundary. The region is split into pieces only where a piece is
  exactly a steer block (successive appends, with an optional notice in between). Every byte of the
  region stays protected however it is split.
- R2.3 (verbatim, by position). `summary_steer_piece` no longer defangs: the steer's inner text is
  `redact(inner)` only. `_serialize_for_summary` builds the text from segments and returns a
  `SummaryInput` (a str subclass) that carries the exact `(start, end)` of every rendered steer.
  `_bound_summary_input`, `_sample_summary_input` and `_budget_with_protected_steers` reserve those
  spans (`summary_input_spans`). The regex re-find (`_PROTECTED_SPAN_RE`, `protected_steer_spans`) is
  deleted. Any string operation returns a plain str with no spans, so stale positions cannot happen.
  Defang still applies to ordinary material: tool bodies, other messages, tool args and the previous
  summary. The budget and the refuse-when-steers-alone-exceed rule are unchanged.

## Deviation (routed to lead): ruling item 1, last sentence, not implemented

"When the FIRST record is written to a row that already has content, the writer runs the legacy parser
once over that content and records any legacy spans it finds." I implemented this and it made the
reviewer's unchanged R2.1 probe fail 2/2. That probe's row (fresh tool output ending in a complete
lookalike block, then the first recorded append) is byte-identical in content and metadata to a
legacy row that received an unrecorded steer before its first recorded append. No available signal
separates the two cases: the persisted marker and the row id are the same in both, and legacy
appends were written through the same flush. The ruling requires both "probe passes unchanged" and
this sentence, and no implementation can satisfy both. I kept the probe (the reviewer's reproduced
P1), so on a legacy row upgraded by its first recorded append, the older unrecorded steer is treated
as tool output. It stays in the transcript and in the tool body but is prunable. It only applies to
rows that were mid-turn when the fork upgraded, a one-off window. `test_legacy_row_upgraded_by_first_recorded_append`
pins this (labelled DEVIATION PIN, not RED evidence). Lead to rule. The alternative is a version
stamp written by the flush on every new tool row. That is a metadata write on every row, and I held
off because the byte-snapshot probe (d79ed7c3) would change.

## Variant table (RED = r2 head 9da323ec production + these tests; GREEN = 2a779043)

| Finding | Variant | Red 9da323ec | Green | Test |
|---|---|---|---|---|
| R2.1 | reviewer: fake tail + new recorded steer (string, list) | yes | yes | r2 probe new_recorded_append[string/list] |
| R2.1 | fake tail + notice + steer, split exact (string, list) | yes | yes | test_fake_tail_then_notice_then_steer |
| R2.1 | prune then re-prune: fake gone, both steers kept, stable | yes | yes | test_prune_then_reprune_drops_fake_keeps_steer |
| R2.1 | list serializer: tool lookalike stays ordinary | yes | yes | test_list_serializer_keeps_fake_as_output |
| R2.1 | static fallback with notice (string, list) | yes | yes | test_static_fallback_keeps_fake_out_of_steers_with_notice |
| R2.1 | records no longer match: keep whole tail (string) | yes | yes | test_unmatched_records_keep_whole_string_tail |
| R2.1 | records no longer match: keep trailing blocks (list) | yes | yes | test_unmatched_records_keep_trailing_text_blocks |
| R2.1 | malformed records are not "no records" | yes | yes | test_malformed_records_are_not_ignored |
| R2.1 | fake block mid-body (string, list) | no (DESIGN GUARD) | yes | test_fake_block_mid_body_is_output |
| R2.2 | reviewer: two quoted blocks, prefix kept | yes | yes | r2 probe two_quoted_blocks[False] |
| R2.2 | two complete quoted blocks + suffix | yes | yes | test_two_complete_quoted_blocks_keep_prefix |
| R2.2 | three quoted openings | yes | yes | test_three_quoted_openings_keep_prefix |
| R2.2 | static fallback carries the whole legacy steer | yes | yes | test_static_fallback_carries_whole_legacy_steer |
| R2.2 | successive legacy appends + notice stay separate | no (DESIGN GUARD) | yes | test_successive_legacy_appends_with_notice |
| R2.2 | legacy tool OPEN before a steer keeps more | no (DESIGN GUARD) | yes | test_legacy_marker_in_tool_output_keeps_more |
| R2.3 | reviewer: quoted markers verbatim (string, list) | yes | yes | r2 probe recorded_quoted_steer[string/list] |
| R2.3 | single-exchange serializer (string, list) | yes | yes | test_single_exchange_serializer_verbatim |
| R2.3 | secret redacted, markers verbatim | yes | yes | test_secret_redacted_markers_verbatim |
| R2.3 | quoted CLOSE inside steer keeps whole reservation (bound, sample) | yes | yes | test_quoted_close_inside_steer_keeps_whole_reservation |
| R2.3 | ordinary lookalikes defanged, no reservation | no (DESIGN GUARD) | yes | test_ordinary_lookalikes_defanged_no_reservation |

RED run (r2 production, throwaway worktree, import path verified): `24 failed, 70 passed in 14.88s`.
All 24 are r3 variants or r2-probe cases. Two of the 24 fail on r2 for reasons that are not the
defect: `test_string_operations_drop_positions` (ImportError, labelled DESIGN GUARD) and
`test_legacy_row_upgraded_by_first_recorded_append` (DEVIATION PIN). Neither is in the table as RED.

## Raw lines

- Reviewer r2 probe, unchanged (cmp against archive): at 9da323ec `5 failed, 2 passed in 3.15s`; at head `7 passed in 3.50s`.
- Round-1 probes unchanged (probes, bytes, guards): `16 passed in 3.76s`. Byte snapshot sha256
  `d79ed7c3b4df3cab55a57e108b446dbd9169252ea510f627078b81f9f93e72fd` (matches).
- Mutant selection (r2 probe + r3 + classes + durable + r1 probes + guards). Control: `94 passed in 15.24s`.
  - (a) marker parsing after consumed records (string branch): `5 failed, 89 passed`. KILLED (r2 probe new_recorded_append[string], ...)
  - (a') the same in the list branch (control `64 passed`): `4 failed, 60 passed`. KILLED (r2 probe new_recorded_append[list], ...)
  - (b) CLOSE as a boundary (the r2 rfind/grow loop restored): `6 failed, 88 passed`. KILLED (r2 probe two_quoted_blocks[False], ...)
  - (c) defang applied to steer text: `7 failed, 87 passed`. KILLED (r2 probe recorded_quoted_steer[string/list], ...)
  - (d) regex re-find of steers in serialized text: `3 failed, 91 passed`. KILLED (quoted_close_inside_steer[bound/sample], ...)
- 138-file set plus r3 tests, r2 probe and bytes probe (HERMES_TEST_WORKERS=2, run once):
  `=== Summary: 141 files, 1787 tests passed, 1 failed, 2 skipped (100% complete) in 121.6s (2 workers) ===`.
  The only failure is `test_review_bytes.py` with `KeyError: 'REVIEW_SNAPSHOT'`: the stock runner drops
  that env var. The file is outside the 138 set and passes standalone with the sha above.
  1787 = 1755 (138 set, builder baseline, 0 failed) + 25 (r3) + 7 (r2 probe).
- Logs: /Users/abbhinnav/.hermes/cache/scratch/p0r3/ (base-probe.log, redmut.log, red.log, green.log,
  mut-*.log, a2-*.log, r1-probes.log, r2-probe-final.log, suite/suite.log).
