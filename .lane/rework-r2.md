# Rework round 2 @ 3d9ee3c19b (fix base f238e3391f, review r1 BLOCK F1-F4)

All production changes are in 3d9ee3c19b: agent/memory_manager.py, plugins/memory/harso/__init__.py,
tools/mcp_tool.py, acp_adapter/server.py. Tests: tests/plugins/memory/test_harso_copilot_r2.py (new) plus one
assertion change in test_harso_copilot.py (the general sanitizer no longer strips the copilot note; that is F1's
ruling). Logs are in .lane/r2-logs/ and .lane/k-mut-r2.json.

## Requirements (restated) and classes

**F1 (P2): switch OFF must leave the whole existing path byte-identical, not just skip the fetch.**
- Class: a copilot-only rule was added to a shared, unconditional sanitizer (`_INTERNAL_NOTE_RE`). Every consumer of
  `sanitize_context` changed behaviour with the switch off.
- Root cause: switch-on behaviour lived in a shared, switch-independent regex.
- Fix: `_INTERNAL_NOTE_RE` is base's again. A separate `_COPILOT_NOTE_RE` runs only inside
  `build_memory_context_block` when a note is passed, which only happens on the enabled copilot path.
- Consumers of the reverted rule: the compose/fence path, `hermes_state` reload, the TUI compare, the stream-scrubber
  fallback, gateway descriptions and run_agent's sidecar check.

**F2 (P1): with the switch on, every model-visible memory representation must be marker-free as the model sees it.**
- Class: sanitizing parts is not the same as sanitizing the whole. Joining (`citation + " " + text`) or JSON
  serialization (quotes and `": "`) can re-form a marker from clean parts. The legacy fallback was not sanitized at all.
- Fix:
  - Legacy fallback: the ASSEMBLED string goes through `_neutralize`, which sanitizes to a fixed point, re-checks with
    `find_markers`, and fails closed to the hint. This runs only when the switch is on.
  - Tool result: `_sanitize_tree` also guards any key, string, or key/value pair whose JSON form carries a marker. It
    wraps the item in the rule's neutral text. `_tool_result_json` then re-checks the FINAL serialized string and
    withholds the result if any marker survives.
  - Deliveries: body lines are re-checked after rendering.
  - Ordinary results stay byte-equal after parsing (tested). Switch-off paths never call any of this.

**F3 (P1): the fail-closed status gate must run before any memory representation is rendered.**
- Class: a new representation was added ahead of an existing denial gate. Siblings: late fetch and tool results.
- Fix: `_recall_denied()` (explicit `recall_status` wins; otherwise legacy `degraded`) runs first in `prefetch`. It is
  also applied to a late-fetch response and to a tool response that carries a status. Denied responses return
  routing_hint-only output, as they do today. The accepted ok/degraded statuses are unchanged.

**F4 (P2): the switch, the advertised tool surface and routing must never disagree. On disable, session_search
comes back.**
- Class: two authorities read the switch at different times. The schemas were read live on every call, while
  routing was indexed once in `add_provider`.
- Fix: one refresh boundary and one routing authority.
  - The provider latches its tool surface (`_tools_on`).
  - `inject_memory_provider_tools` calls `MemoryManager.refresh_tool_routing()`, which re-indexes the existing
    `_tool_to_provider` table for a provider whose latch changed. The shared rule is `_index_provider_tools`, and
    there is no second dispatcher.
  - The surface is then reconciled against that same index: stale injected tools are removed with their route.
    `reconcile_displaced_tools` displaces session_search only while harso_memory is on the surface AND routed, and
    restores it at its original index.
  - `refresh_agent_mcp_tools` keeps the displacement.
  - ACP `/tools` (a read-only listing) cannot move routing.
  - The switch takes effect at the next refresh boundary: agent construction (the gateway builds per message) or
    ACP's explicit rebuild. See deviations.md item 8. This protects the prompt cache (AGENTS.md).

## Variant table (RED = production files of f238e339 written over the fix via git show, then restored and cmp'd)

Script: .lane/k_red_green_r2.sh. Log: .lane/r2-logs/k-r2-redgreen.log.
- RED: `67 failed, 11 passed`. The 11 that passed are the reviewer's 4 positive controls plus 7 of my
  controls/usability checks.
- GREEN: `78 passed`.

| F | Variant | Red f238e339 | Green 3d9ee3c1 | Test |
|---|---|---|---|---|
| F1 | reviewer fixed input == base bytes (expected = reviewer's base log) | FAIL | pass | test_f1_reviewer_fixed_input_matches_base_bytes |
| F1 | note alone / mid-text / uppercase / wrapped line in general sanitizer (4) | FAIL x4 | pass | test_f1_general_sanitizer_is_base |
| F1 | default fence block keeps note text | FAIL | pass | test_f1_default_fence_block_keeps_copilot_note_text |
| F1 | sibling site: SessionDB reload | FAIL | pass | test_f1_session_reload_keeps_copilot_note_text |
| F1 | enabled note round-trip kept (control) | pass | pass | test_f1_enabled_fence_path_still_strips_echoed_copilot_note + test_fence_note_survives_... |
| F2 | fallback text/citation/citations x 8 rule-table markers incl. fullwidth, unicode hyphen, zero-width (24) | FAIL x24 | pass | test_f2_fallback_fields_hold_the_whole_rule_table |
| F2 | marker formed only by the citation+text join | FAIL | pass | test_f2_marker_split_across_citation_and_text_join |
| F2 | tool JSON: casefold key/value, unicode hyphen, nested deep key, key+sibling, space-form key, zero-width key (6 of 7) | FAIL x6 | pass | test_f2_final_tool_json_has_no_marker |
| F2 | ordinary tool result unchanged; gaps/hint usable (controls) | pass | pass | test_f2_ordinary_tool_result_is_unchanged, test_f2_gaps_and_hint_lines_stay_usable |
| F3 | contradictory envelopes: denied, "", "OK", 1, ["ok"], unavailable+degraded=false (6) | FAIL x6 | pass | test_f3_contradictory_envelope_never_renders_delivery_or_items |
| F3 | denial keeps routing_hint only | FAIL | pass | test_f3_denied_envelope_keeps_routing_hint_only |
| F3 | sibling: late fetch obeys gate (2) | FAIL x2 | pass | test_f3_late_fetch_obeys_the_same_gate |
| F3 | sibling: tool result with denial status withheld | FAIL | pass | test_f3_tool_result_with_denial_status_is_withheld |
| F4 | between boundaries: surface + routing hold still | FAIL | pass | test_f4_between_boundaries_surface_and_routing_hold_still |
| F4 | ON surface, switch OFF before refresh: still routable | FAIL | pass | test_f4_hot_disable_before_refresh_keeps_harso_memory_routable |
| F4 | same manager OFF->ON->OFF restores today's surface in order | FAIL | pass | test_f4_off_on_off_round_trip_restores_todays_surface_in_order |
| F4 | sibling boundary: refresh_agent_mcp_tools keeps swap + route | FAIL | pass | test_f4_mcp_rebuild_keeps_the_swap_and_routing |
| F4 | idempotent refresh; ACP /tools listing cannot move routing (controls) | pass | pass | test_f4_repeated_refresh_is_idempotent, test_f4_read_only_tool_listing_cannot_move_live_routing |

Reviewer probes, rerun UNCHANGED (copied from probes/test_review_probes.py, `cmp` ok):
- RED on f238e339: `15 failed, 4 passed`. This is identical to the reviewer's review-probes.log.
- GREEN: `19 passed`.
- `probe_default_off.py`: RED gives `note_preserved: false`. GREEN gives output byte-identical (`cmp`) to the
  reviewer's `default-off-base.log`, and also to base 84ae89c383 run fresh in a throwaway worktree.
- Switch-off fixed-transcript probe (.lane/k_probe_switch_off.py, 7 requests, base worktree vs fix): sha256
  697069a5f863ad93… on both, BYTE_IDENTICAL=True.

## Mutants

Throwaway detached worktree of 3d9ee3c1; driver .lane/k_mut_r2.py; results .lane/k-mut-r2.json.
- Import check: both `agent.memory_manager` and `plugins.memory.harso` resolved from the mutant tree.
- Selection: test_harso_copilot, test_harso_copilot_wire, test_harso_render, test_harso_copilot_r2 and the reviewer
  probes.
- Every control was GREEN (`767 passed, 1 xfailed`) immediately before its mutant. The tree was restored clean after
  each one.

| Mutant | Result | Killing test (first) |
|---|---|---|
| F1 copilot note back in general sanitizer | 8 failed KILLED | test_f1_reviewer_fixed_input_matches_base_bytes |
| F2a fallback not neutralized | 31 failed KILLED | test_f2_fallback_fields_hold_the_whole_rule_table |
| F2b tool JSON assembly unguarded | 3 failed KILLED | test_f2_final_tool_json_has_no_marker |
| F3 delivery before status gate | 11 failed KILLED | test_f3_contradictory_envelope_never_renders_delivery_or_items |
| F4 schemas read live switch | 1 failed KILLED | test_f4_between_boundaries_surface_and_routing_hold_still |
| F4b session_search not restored | 2 failed KILLED | test_f4_off_on_off_round_trip..., reviewer test_hot_disable_restores_search_backstop |
| m1 memory into content | 2 failed KILLED | test_mid_turn_block_goes_to_sent_bytes_only |
| m2 switch ignored | 16 failed KILLED | test_switch_is_off_unless_literally_true |
| m3 switch truthy | 2 failed KILLED | test_switch_is_off_unless_literally_true[yes-please/1] |
| m4 summarizer strip off | 1 failed KILLED | test_t11_summary_input_has_steer_and_no_delivery_text[llm] |
| m5 pre_compress strip off | 1 failed KILLED | test_t11_compaction_path_strips_only_when_switch_on |
| m6b mid-turn unsanitized | 1 failed KILLED | test_mid_turn_block_goes_to_sent_bytes_only |
| m7 acks dropped | 2 failed KILLED | test_switch_on_turn_post_acks_... |
| m8 finalized_items read sidecar | 2 failed KILLED | test_switch_on_turn_post_acks_..._t12 |
| m9 displacement without harso_memory routed on surface | 1 failed KILLED | test_session_search_stays_when_switch_on_but_harso_memory_absent |
| m10 rule row renamed | 37 failed KILLED | test_t5_rows_cover_the_rule_table_exactly |

m9 was re-expressed for the new code: the mutant removes the "routed tool on this surface" guard in
`MemoryManager.displaced_tool_names`. It is still killed by its original test, so the broader F4 rule does not mask it.

## Scoped suite (ONE run)

Log: .lane/r2-logs/k-r2-scoped2.log, HEAD 3d9ee3c1, throwaway HERMES_HOME.
- Selection: tests/plugins/memory/ (with the reviewer probes), tests/agent/test_memory_provider.py,
  test_builtin_memory_disabled_surface.py, test_pre_compress_memory_context.py, tests/agent/test_context_compressor*.py,
  tests/run_agent/test_memory_provider_init.py, tests/tools/test_refresh_agent_mcp_tools.py, tests/acp/test_server.py.
- Result: `1660 passed, 2 skipped, 1 xfailed in 40.87s`, EXIT 0, 0 FAILED/ERROR lines. The xfail is the strict P0
  marker, which stays.
- The repo has no tests/agent memory_manager or memory_delivery test files; their coverage is in the files above.
- An earlier attempt (.lane/r2-logs/k-r2-scoped.log) used `-n 4` without xdist installed. pytest exited 4 and ran
  ZERO tests. It is not counted.
- The whole fork suite was not rerun.
