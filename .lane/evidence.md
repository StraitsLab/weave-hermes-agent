# P0 durable tool-row appends — evidence

## Focused suite base (lead-provided, base 84ae89c383, pre-edit)
434 passed, 2 skipped in 19s

## RED on base (84ae89c383 code, tests/agent/test_durable_tool_append.py) — 14 failed, 1 passed
- T0a post-batch / pre-API / wrap-up: live send carries text, stored row does not (assert stored == live fails).
- T0c x3: list tool content flattened to 'Image attached natively\n[screenshot]' on persist; pre-API helper absent (ImportError).
- T0d list: flattened. (T0d string passes on base: flush of an unsaved row already carries the append.)
- T0e: steer appended in memory even though durable write is impossible (no guard).
- T0b x6: prune / second prune / pressure / lean demotion / LLM serializer truncation / static fallback all drop the steer.
- 03:39 GREEN: tests/agent/test_durable_tool_append.py 15 passed (helper agent/tool_row_append.py + SessionDB.append_to_tool_message + compressor steer carry)

## Mutants (run in detached copy P0-MUTANTS @ 0619b34031; each reverted after)
| id | edit site | red tests |
|---|---|---|
| m1 | agent/tool_row_append.py append_to_tool_row: replace `db.append_to_tool_message(...)` with `updated = 1` | 7 red: T0a x3 (post-batch, pre-API, wrap-up), T0c x3, T0e |
| m2 | agent/context_compressor.py _demote_tool_result_at: `with_steers(summary, steers)` -> `summary` | 3 red: T0b prune, second-prune, pressure demotion |
| m3 | agent/tool_row_append.py _appended_content: list -> `str(existing) + suffix` | 4 red: T0c x3, T0d list |

## Noise
contributors/emails/agent@Agents-Mac-mini.local: the repo tracks two paths that differ only by case (agent@Agents-... and agent@agents-...). The APFS volume is case-insensitive, so the file shows dirty in every fresh checkout (it does in P0-MUTANTS too) and `git checkout --` cannot clean it. Left unstaged; never committed.

## Matching-set suite (scripts/run_tests.sh -j 4, per-file isolation; venv = CI `uv sync --locked --python 3.11 --extra all --extra dev --extra anthropic --extra mistral --extra fal --extra modal --extra daytona --extra hindsight --extra parallel-web`)
File list: every tests/**/*.py whose name matches steer|compress|compaction|hermes_state|transcript_repair|tool_executor|run_budget (131 files; list in scratch p0_files.txt).
- BASE 84ae89c383 (detached copy P0-BASE-84ae89c383): 131 files, 1542 passed, 0 failed, 2 skipped
- HEAD 0619b34031 (+ tests/agent/test_durable_tool_append.py): 132 files, 1557 passed, 0 failed, 2 skipped  (= 1542 + 15 new)
- New failures vs base: none.
- Earlier attempt printed only "ERROR: usage" (-n 8, xdist not installed) and then exited 2 at collection (aiohttp missing before the CI uv sync). Neither of those runs executed any tests.
## Focused suite at head: 434 passed, 2 skipped (same as base)
- 03:55 full suite (scripts/run_tests.sh -j 6) running: base in P0-BASE copy, then head in P0-MUTANTS copy @0619b34031 (clean except case-collision noise file)

## Full suite (scripts/run_tests.sh -j 6, CI-extras venv)
- BASE 84ae89c383 (P0-BASE copy): 3532 files, 42800 passed, 42 failed, 444 skipped
- HEAD 0619b34031 (P0-MUTANTS copy, clean): 3533 files, 42815 passed, 42 failed, 444 skipped
- Failing test NAMES (from FAILED lines): base 42, head 42, identical sets. New at head: none. Fixed at head: none.
  The pre-existing failures are in hermes_cli update/dashboard, gateway readiness/scale_to_zero/systemd/buzz, computer_use, tools voice/wake_word/execution_flag and similar; none are in the areas this packet touched.

## Design notes for review
- One helper: agent/tool_row_append.append_to_tool_row (all 3 append points). Guarded SessionDB.append_to_tool_message:
  UPDATE ... WHERE id=? AND session_id=? AND role='tool' AND active=1 AND tool_call_id IS ? AND content IS <encoded pre-append>.
  List content is compared by exact _encode_content JSON (acts as the digest).
- Refusal: row untouched, log once per agent, steer re-queued ahead of newer pending steer (the post-batch drain, the pre-API drain, or the turn-end result["pending_steer"] fallback delivers it). Wrap-up refusal leaves the latch open so it retries.
- Persisted flag on a row with no _row_id (e.g. replayed history) -> refuse and re-queue. A row with no persisted marker -> in-memory only; the next flush carries it (T0d).
- No schema change: tool-row block lists now persist structurally via the existing _encode_content JSON path (`\x00json:` prefix), so no migration or upgrade test is needed. Non-tool roles keep the old text flattening. Shared owner: persisted_message_content().
  Cost: the flush now stores base64 image blocks of tool rows in state.db, where it used to store '[screenshot]'. Flag for the reviewer (DB size).
- Steer tracking: the existing marker text only (STEER_MARKER_OPEN..CLOSE peeled from the END of tool content; the wrap-up notice is peeled and dropped). No new persisted marker. It survives reload because it is part of the content.
- Pre-existing, out of scope: replayed tool messages lack the OpenAI `name` field the live send carries (true with or without a steer).

## Rework round 2 (fix base 0619b34031; detail in .lane/rework-r2.md)
- Shared split/reattach authority (agent/tool_row_append.py) + persisted span records (display_metadata.tool_appends) used by every tool-content rewrite.
- Reviewer probes, unchanged (cmp == archive): RED 8 failed / 7 passed on 0619b340 production files; GREEN 15 passed at head.
- Class variants tests/agent/test_durable_tool_append_classes.py: RED 28 failed / 4 passed (the 4 are labelled design guards); GREEN 32 passed.
- Mutants mA mB mC mD mE1 mE2 m1 m2 m3: all KILLED (unmutated 62 passed each, throwaway worktrees, 0 left).
- No-append byte snapshot SHA256 d79ed7c3b4df3cab55a57e108b446dbd9169252ea510f627078b81f9f93e72fd (== reviewer base/head).

## Suite — r2, run once (scripts/run_tests.sh, HERMES_TEST_WORKERS=2, scratch HERMES_HOME)
Files: the 131-file matching set + six named focused files + test_durable_tool_append.py + test_durable_tool_append_classes.py + vendored reviewer probes/guards = 138 files.
=== Summary: 138 files, 1755 tests passed, 0 failed, 2 skipped (100% complete) in 108.1s (2 workers) === (exit 0)
Reconciles with the reviewer's 135-file head run (1708) + 32 variants + 8 probes + 7 guards = 1755. Full fork suite not rerun (per dispatch).
