# P0-TOOL-APPEND — rework round 2 (fix base 0619b34031fe82acbed4565add17f76b53f5029b)

Reviewer: GPT cross-family, round-1 BLOCK, one finding F1 with five sub-findings (all accepted as valid; lead rulings followed).

## Requirement (restated)
Once text has been delivered to the model as a user /steer on a tool row, every later rewrite of that tool content keeps it whole. That holds for every content shape (string, block list, image list) and every rewrite: prune, pressure/lean demotion, dedup back-reference, last-resort salvage, both compaction serializers, the static fallback, and final summarizer-input budgeting. The only permitted change is established secret redaction. The wrap-up notice stays disposable. When protected text cannot fit an existing bound, the result is *no rewrite*, never a lossy one.

## Class
**Class:** each tool-content rewrite site decided on its own how to find and handle appended text. Some worked on a derived representation (the joined string, the whole serialized input), so any site that clipped, joined, sampled or placeholdered before splitting lost the steer.
**Root cause:** there was no single authority for "which part of this row is appended, protected text". The round-1 fix peeled markers from one string representation at one or two sites, and marker parsing alone was ambiguous (rfind on a quotable delimiter).
**Fix shape (single authority):** `agent/tool_row_append.py`
- `append_to_tool_row` records each append's span (`{"kind": steer|notice, "chars": exact length}`) in the row's `display_metadata["tool_appends"]`. For a saved row it is written in the same guarded `SessionDB.append_to_tool_message` transaction; for an unsaved row the normal flush writes it. No schema change: the column already exists and is stripped from every provider request (conversation_loop.py:2320).
- `split_trailing_appends(content, metadata)` / `split_tool_message(msg)` split the ORIGINAL structured content. Span records are used when they match. Record-less legacy rows fall back to marker parsing, conservatively: an opening marker with no closing marker before the next one is treated as quoted, so the protected span grows outward and the user's prefix is never cut.
- `with_steers` re-attaches pieces unclipped. `summary_steer_piece` renders a steer for summarizer input (redacted, unclipped). `defang_steer_markers` neutralises markers in all *ordinary* summarizer-input material, so in a serialized input a live OPEN..CLOSE block can only be a protected steer. `protected_steer_spans` finds those blocks for budgeting.
- Every rewrite site now calls `split_tool_message`: `_demote_tool_result_at` (prune + pressure), `_demote_stale_tail_tools` (lean), pass-1 dedup (sibling found during the class sweep), `salvage_grown_transcript`, `_serialize_for_summary` (and `_serialize_one_exchange` through it), `_build_static_fallback_summary`, and `_bound_summary_input`/`_sample_summary_input` through `_budget_with_protected_steers`.

## Per sub-finding
| Sub | Site(s) | Fix |
|---|---|---|
| F1.a list/image serializer | context_compressor `_serialize_for_summary` | split from the original list before join/redact/clip; only the body is rendered and bounded; steers re-attached redacted and unclipped |
| F1.b static fallback | `_build_static_fallback_summary` | no 700-char clip, no whitespace fold, no first-eight limit; all steers oldest→newest are kept inside the SAME `_FALLBACK_SUMMARY_MAX_CHARS`, and disposable sections pay the truncation. If the steers alone cannot fit it returns `None`, and `compress()` then keeps the transcript (`_last_compress_aborted`, failure_class `protected_steer_overflow`) |
| F1.c salvage | `salvage_grown_transcript` | placeholder + `with_steers` through the shared split; the under-budget acceptance check is unchanged, so no salvage happens if the protected content cannot fit |
| F1.d input budgeting | `_bound_summary_input`, `_sample_summary_input`, `_generate_summary` | protected spans plus worst-case elision markers are reserved inside the same 160K bound, and only ordinary material is head/tail-clipped or even-sampled (same policies, ordinary-only coordinates). Returns `None` when the steers alone exceed the bound; `_generate_summary` then makes no call and returns `None` (same abort rule). The previous-summary block is defanged before its own bound |
| F1.e quoted marker | `split_trailing_appends` | persisted span records (brief rule 4) make the span exact; the legacy marker fallback retains ambiguous content instead of cutting at an inner quoted marker |

## Variant table
Tests: reviewer probes vendored unchanged as `tests/agent/test_review_probes.py` and `tests/agent/test_review_guards.py` (cmp == the archive copies); variants in `tests/agent/test_durable_tool_append_classes.py`. RED = production files `agent/tool_row_append.py`, `agent/context_compressor.py`, `hermes_state.py` overwritten with `git show 0619b340:<file>`, then restored and `cmp`-checked (script `scratch/p0r2/redgreen.sh`, log `redgreen.log`; no git stash).

### Rework round 2 @ f6ecbd62f667f7c1d2fe02b13e3aa518db32f422 (fix base 0619b34031)
| Finding | Class | Variant | Red on 0619b340 | Green on f6ecbd62 | Test |
|---|---|---|---|---|---|
| F1.a | split after join | reviewer: list steer post-batch / pre-api | yes | yes | test_review_probes::test_list_steer_reaches_llm_serializer_whole[x2] |
| F1.a | | two list steers + wrap-up notice block | yes | yes | TestF1aStructuredSerializer::test_two_list_steers_and_notice_all_steers_whole |
| F1.a | | secret in a long list steer: redacted, rest whole | yes | yes | ::test_list_steer_secret_redacted_but_rest_whole |
| F1.a | | lookalike marker inside the list body is defanged, not protected | yes | yes | ::test_list_body_lookalike_marker_is_defanged_not_protected |
| F1.a | | sibling path: `_serialize_one_exchange` (micro-compaction) | yes | yes | ::test_single_exchange_serializer_keeps_list_steer |
| F1.a | | real drain → SessionDB reload, multiline+long steer | yes | yes | ::test_real_drained_list_row_reloaded_keeps_multiline_steer |
| F1.b | clip/limit on protected text | reviewer: long steer; ninth/newest steer | yes | yes | test_review_probes::test_static_fallback_* (x2) |
| F1.b | | 20 steers all kept, output ≤ bound, disposable text truncated | yes | yes | TestF1bStaticFallback::test_twenty_steers_all_kept_within_bound |
| F1.b | | multiline steer verbatim (no whitespace fold), secret redacted | yes | yes | ::test_multiline_steer_kept_verbatim_and_secret_redacted |
| F1.b | | steers alone exceed bound → None | yes | yes | ::test_steers_that_cannot_fit_return_none |
| F1.b | | list-row steer reaches fallback whole | yes | yes | ::test_list_row_steer_reaches_fallback_whole |
| F1.b | | compress() end-to-end keeps every steer when fallback cannot fit | yes | yes | ::test_compress_keeps_transcript_when_steers_cannot_fit |
| F1.c | placeholder without reattach | reviewer: salvage erases steer | yes | yes | test_review_probes::test_salvage_keeps_steer_on_old_tool |
| F1.c | | two steers kept, notice dropped | yes | yes | TestF1cSalvage::test_two_steers_kept_notice_dropped |
| F1.c | | protected steer too big → no salvage (or kept whole) | yes | yes | ::test_protected_steer_that_cannot_fit_means_no_salvage |
| F1.c | | quoted-marker steer survives salvage | yes | yes | ::test_quoted_marker_steer_prefix_survives_salvage |
| F1.c | | real drained DB row salvaged then re-split exactly | yes | yes | ::test_real_drained_row_salvaged_then_resplit_exactly |
| F1.c | sibling | dedup back-reference of an older duplicate body keeps its steer | yes | yes | TestF1SiblingDedup::test_older_duplicate_body_keeps_its_steer |
| F1.d | clip whole serialized string | reviewer: 60 steers, bound + sample | yes | yes | test_review_probes::test_final_summary_input_budget_keeps_steer[x2] |
| F1.d | | output ≤ the same 160K bound with all steers (x2 methods) | yes | yes | TestF1dInputBudget::test_output_stays_within_same_bound[x2] |
| F1.d | | list/image rows (x2) | yes | yes | ::test_list_rows_steers_survive_budget[x2] |
| F1.d | | long multiline steers whole, secret redacted (x2) | yes | yes | ::test_long_multiline_steers_whole_and_redacted[x2] |
| F1.d | | protected alone over bound → None (x2) | yes | yes | ::test_protected_alone_over_bound_returns_none[x2] |
| F1.d | | `_generate_summary` makes zero summarizer calls then | yes | yes | ::test_generate_summary_makes_no_call_when_steers_cannot_fit |
| F1.e | ambiguous delimiter | reviewer: quoted opening marker | yes | yes | test_review_guards::test_quoted_steer_marker_does_not_erase_user_prefix |
| F1.e | | drain persists span record (DB == live metadata) | yes | yes | TestF1eSpanIdentification::test_drain_persists_span_records |
| F1.e | | record-driven prune of a quoting steer after reload | yes | yes | ::test_records_prune_quoting_steer_whole |
| F1.e | | legacy (record-less) quoted OPEN+CLOSE inside steer | yes | yes | ::test_legacy_quoting_open_and_close_keeps_everything |
| F1.e | | list row, long prefix before quoted marker, via real drain | yes | yes | ::test_list_row_quoting_steer_via_drain_serializes_whole |

Design guards (green on both; they protect the r2 mechanism and are **not** counted as RED variants): `test_records_are_exact_when_tool_output_ends_with_open_marker`, `test_repeated_prune_with_records_is_stable`, `test_tool_output_lookalike_blocks_get_no_reservation[x2]`.

Raw counts (redgreen.log):
- RED @0619b340 production files: reviewer probes **8 failed, 7 passed** (the exact 8 from the review); class variants **28 failed, 4 passed** (the 4 passing are the labelled design guards).
- GREEN @HEAD: reviewer probes **15 passed**; class variants **32 passed**.
- Restore: `cmp` ok for all three files; `git diff --quiet HEAD` on them.

## Mutants (throwaway detached worktrees at f6ecbd62, script `scratch/p0r2/mutants.sh`, log `mutants.log`)
Selection: test_durable_tool_append.py + test_durable_tool_append_classes.py + test_review_probes.py + test_review_guards.py. Each worktree was verified to import its own `agent` package, ran green unmutated (62 passed), and was then mutated.
| id | sub | edit | unmutated | mutated | verdict |
|---|---|---|---|---|---|
| mA | F1.a | serializer: skip `split_tool_message` for tool rows | 62 passed | 20 failed | KILLED |
| mB | F1.b | fallback: clip steer to 700 chars | 62 passed | 4 failed | KILLED |
| mC | F1.c | salvage: placeholder without `with_steers` | 62 passed | 5 failed | KILLED |
| mD | F1.d | budget: never route to protected-steer reservation | 62 passed | 11 failed | KILLED |
| mE1 | F1.e | marker fallback: drop the conservative outward growth | 62 passed | 3 failed | KILLED |
| mE2 | F1.e | span record never written | 62 passed | 2 failed | KILLED |
| m1 | orig | durable DB update bypassed (`updated = 1 or …`) | 62 passed | 17 failed | KILLED |
| m2 | orig | prune: `summary` without steers | 62 passed | 7 failed | KILLED |
| m3 | orig | list append stringified | 62 passed | 4 failed | KILLED |
All mutant worktrees were removed afterwards (0 remaining).

## Unchanged behaviour without appends
The reviewer's `test_review_bytes.py` snapshot (plain-string rows / serializer / static fallback / prune, no appends) at HEAD gives SHA256 `d79ed7c3b4df3cab55a57e108b446dbd9169252ea510f627078b81f9f93e72fd`, identical to the reviewer's base and head values. Defanging only rewrites text that contains the exact steer marker.

## Deviations / notes
- `_build_static_fallback_summary`, `_bound_summary_input` and `_sample_summary_input` may now return `None`, but only when protected steers alone exceed the existing bound. Every production caller handles it: `compress()` aborts unchanged; `_generate_summary` returns `None` with no request; the previous-summary bound is defanged first, so its result is never `None`.
- `display_metadata.tool_appends` rides the existing column. Readers that only use known keys (reactions, task_count) are unaffected.
- Remaining residual (not reproduced, stated plainly): a record-less legacy row whose TOOL OUTPUT itself ends in a complete fake steer block still looks like a steer. That is the round-1 behaviour and errs toward retention. Every append made through the helper from now on is record-identified.

## Suite (once, at the end)
See the "Suite" section in .lane/evidence.md.
