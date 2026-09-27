VERDICT: BLOCK — P0-TOOL-APPEND at d62c2d86ee8ddbbbacb440a35de5bfdb0eb93b15

ESCALATE: design — @hermes. One reproduced P2 MUST-FIX remains in the scoped legacy-delimiter/data-fidelity class. The original R3.1 notice deletion is ADDRESSED, but the adjacent-marker sibling still deletes actual user-authored bytes in deterministic fallback. This is not fail-safe over-retention. Route to the lead, not another builder review cycle.

Scope and evidence provenance

Independent GPT-family reviewer; builder and lead Claude Opus per dispatch. Reviewed the exact diff 5bcf324b22ff6cfa285d96317eea72b529762c40..d62c2d86ee8ddbbbacb440a35de5bfdb0eb93b15. Both lane and detached review HEAD were computed, not inferred. Read .lead-brief.md first, attempted .lane/preflight.md (absent), read ruling-r4.md, rework-r4.md, deviations.md, archived review-gate-r3.md, archived r2/r3 probes, the complete helper, changed tests and affected consumer paths. Also inspected origin/main:AGENTS.md; docs/packet-protocol.md was absent in the fork's named fix-base tree. No claim of a fresh Cloud ADR audit is made for this scoped fork delta.

Source findings below are proven at the frozen source; test counts, hashes and outputs are observed execution. Builder's 143-file/1814-passing scoped result remains reported, not independently rerun. Per the latest scoped dispatch, only tool-append tests and reviewer probes were rerun, not the repository-wide suite. No live DB, deployment or model-summary quality claim is made.

Prior finding disposition

R3.1: ADDRESSED. Unchanged r3 notice-loss probe: 8/8 green at head; independently 8/8 red at named round-3 base under an outer timeout. Unchanged r3 hunt: 8/8 green. Unchanged r2 boundaries, abort, probes, guards and byte snapshot: green. D2's rewritten genuine-successive-legacy-notice expectation matches the ruling; no attempt was made to restore notice deletion.

REPRODUCED MUST-FIX

R4.1 — P2, data fidelity / acceptance, ambiguous legacy delimiter provenance: splitting a quoted adjacent CLOSE/OPEN pair is lossless only until a downstream consumer unwraps each piece. Deterministic fallback strips the user's quoted markers and replaces their boundary with a bullet separator.

Frozen-source counterexample sites:
  agent/tool_row_append.py:329-338 — the legacy region is split at the user-quoted adjacent marker pair.
  agent/tool_row_append.py:444-451 — steer_texts removes each resulting piece's OPEN/CLOSE as if both were producer-owned wrappers.
  agent/context_compressor.py:4669-4673 — static fallback consumes those unwrapped pieces.
  agent/context_compressor.py:4807-4810 — renders the surviving prefixes as separate bullets.
  agent/context_compressor.py:8373-8392 — production fallback path uses the result; only None triggers safe retention of the original transcript. The reproducer returns a non-None lossy summary.

Minimal valid legacy input (one user steer, wrapped by the real producer):
  user = 'KEEP-PREFIX\n' + STEER_MARKER_CLOSE + '\n\n' + STEER_MARKER_OPEN + '\nKEEP-SUFFIX'
  row.content = BIG_BODY + format_steer_marker(user)
  row has no tool_appends metadata.

The split still satisfies ''.join(pieces) == format_steer_marker(user), and both model-summary serializers preserve user verbatim. But fallback produces:
  EXTRACTED: ['KEEP-PREFIX', 'KEEP-SUFFIX']
  FALLBACK_SECTION: '- KEEP-PREFIX\n- KEEP-SUFFIX'

The missing CLOSE, OPEN and surrounding bytes were inside the user input, not framework wrappers. No secrets or redaction are involved. This is a residual at the named fix base too, not a new-regression claim. It is inside the requested hunt for record-less legacy inputs deleting/reordering user bytes.

Raw reproducer command, cwd /tmp/rv-p0-tool-append-r4/tree:
  /opt/homebrew/bin/timeout 900 /tmp/rv-p0-tool-append-r4/tree/.venv/bin/python -m pytest -q -p no:cacheprovider --tb=short --show-capture=no -s tests/agent/test_review_r4_fallback.py

Environment supplied by run_checks.py: HOME=/tmp/rv-p0-tool-append-r4/home, HERMES_HOME=/tmp/rv-p0-tool-append-r4/home/.hermes, TMPDIR/TMP/TEMP=/tmp/rv-p0-tool-append-r4/tmp, PYTHONDONTWRITEBYTECODE=1. Probe source retained at /tmp/rv-p0-tool-append-r4/test_review_r4_fallback.py.

Raw output excerpt, fallback-head.log:
  SHAPE: string RECORDED: False
  EXTRACTED: ['KEEP-PREFIX', 'KEEP-SUFFIX']
  FALLBACK_SECTION: '- KEEP-PREFIX\n- KEEP-SUFFIX'
  E   AssertionError: user-authored CLOSE and OPEN were removed, not retained or refused
  FAILED tests/agent/test_review_r4_fallback.py::test_fallback_preserves_one_user_quoted_boundary[string-False]
  1 failed, 3 passed in 2.47s
  EXIT: 1

Identical probe at 5bcf324b22ff6cfa285d96317eea72b529762c40, cwd /tmp/rv-p0-tool-append-r4/base:
  /opt/homebrew/bin/timeout 900 /tmp/rv-p0-tool-append-r4/base/.venv/bin/python -m pytest -q -p no:cacheprovider --tb=short --show-capture=no -s tests/agent/test_review_r4_fallback.py
Raw output, fallback-base.log:
  FAILED tests/agent/test_review_r4_fallback.py::test_fallback_preserves_one_user_quoted_boundary[string-False]
  1 failed, 3 passed in 2.37s
  EXIT: 1

Sibling sites / cases checked:
  - Legacy string with no notice, with a quoted notice before the ambiguous pair, and with one after it: all three fallback variants fail.
  - Identical legacy list variants: pass.
  - Real append -> SessionDB reload, recorded string and recorded list: pass; their authoritative span is not split.
  - split, real prune, re-prune, model-summary serializer and single-exchange serializer: pass for the minimal counterexample; the fallback alone unwraps without reconstructing the quoted boundary.
  - Search of steer_texts consumers found fallback and summary_steer_piece. The latter re-wraps each piece, and execution confirms it preserves this boundary. The fallback does not.
  - Builder test_durable_tool_append_r3.py:243-245 explicitly requires two separately unwrapped pieces for genuine successive legacy appends without a notice. Those bytes are indistinguishable from this quotation, so that guard cannot establish user-byte fidelity. It needs reconciliation with the conservative legacy rule, not blind preservation as an invariant.

Variant command, cwd head tree:
  /opt/homebrew/bin/timeout 900 /tmp/rv-p0-tool-append-r4/tree/.venv/bin/python -m pytest -q -p no:cacheprovider --tb=short --show-capture=no tests/agent/test_review_r4_hunt.py
Raw output, hunt-corrected.log:
  FAILED tests/agent/test_review_r4_hunt.py::test_static_fallback_quoted_adjacent_boundary[before-string]
  FAILED tests/agent/test_review_r4_hunt.py::test_static_fallback_quoted_adjacent_boundary[after-string]
  FAILED tests/agent/test_review_r4_hunt.py::test_static_fallback_quoted_adjacent_boundary[none-string]
  3 failed, 7 passed in 14.20s
  EXIT: 1

General rule / smallest fix:
  On a record-less string, an adjacent CLOSE/OPEN pair cannot prove two separately appended steers. Preserve the whole ambiguous region as one protected piece rather than splitting it; allow only its outermost producer wrapper to be removed. This is a smaller, conservative deletion of parsing logic, requires no new metadata/schema/authority, and leaves recorded rows unchanged. Reconcile the legacy no-notice separate-piece guard accordingly. Alternatively, preserve/reconstruct every ambiguous internal marker in fallback, but do not impose global joining on recorded pieces. No fix was applied or validated in the lane.

Executed acceptance and regression checks

1. Exact archived probes copied unchanged

All r2/r3 archived Python files were copied to scratch; existing fixture dependencies were byte-compared and matched. Every executed archived test copy remained byte-identical. Computed hashes and source/copy paths: probe-manifest.json. Original probes were never edited. The new r4 probes are separately named.

2. Clean unmutated selection before mutants

Command, cwd head tree:
  /opt/homebrew/bin/timeout 900 /opt/homebrew/bin/heavy /tmp/rv-p0-tool-append-r4/tree/.venv/bin/python -m pytest -q -p no:cacheprovider -o timeout=120 --tb=short --show-capture=no tests/agent/test_durable_tool_append.py tests/agent/test_durable_tool_append_classes.py tests/agent/test_durable_tool_append_r3.py tests/agent/test_review_r3_notice_loss.py tests/agent/test_review_r3_hunt.py tests/agent/test_review_r2_boundaries.py tests/agent/test_review_r2_abort.py tests/agent/test_review_probes.py tests/agent/test_review_guards.py
Raw control.log:
  122 passed, 1 warning in 21.81s
  EXIT: 0

The warning was Unknown config option: timeout because pytest-timeout is not installed. The actual guard was /opt/homebrew/bin/timeout 900 around every test command; no install was performed. Subsequent commands removed the ineffective pytest option while retaining the outer timeout. Initial import/quoting setup errors were not counted as test results.

3. Standalone byte snapshot

Command, cwd head tree, REVIEW_SNAPSHOT=/tmp/rv-p0-tool-append-r4/bytes.json:
  /opt/homebrew/bin/timeout 900 /tmp/rv-p0-tool-append-r4/tree/.venv/bin/python -m pytest -q -p no:cacheprovider -o timeout=120 --tb=short --show-capture=no tests/agent/test_review_bytes.py
Raw bytes.log:
  1 passed, 1 warning in 1.73s
  EXIT: 0
Computed SHA256:
  d79ed7c3b4df3cab55a57e108b446dbd9169252ea510f627078b81f9f93e72fd
Matches the requested archived snapshot. This test covers no-append behavior, not every recorded-row behavior.

4. Both builder mutants independently reproduced in separate detached trees

Driver: run_mutants.py. Exact edit diffs: mutant-a.diff and mutant-b.diff. Same nine-file 122-test selection as the successful unmutated control, using each mutant tree's Python and source; every command begins /opt/homebrew/bin/timeout 900. Each tree was created at the frozen head with git worktree add --detach. Nothing in the lane was mutated.

(a) Restore notice deletion with region = body[start:].replace(_NOTICE_PIECE, ""):
  19 failed, 103 passed in 19.59s
  EXIT: 1
(b) Restore strip-then-split loop and advance cut past notice bytes:
  19 failed, 103 passed in 23.36s
  EXIT: 1

Both have named FAILED cases, including:
  tests/agent/test_durable_tool_append_r3.py::TestR22LegacyKeepsMore::test_successive_legacy_appends_with_notice
  tests/agent/test_durable_tool_append_r3.py::TestR31LegacyNoticeKept::test_quoted_notice_split_keeps_every_byte[1]
  tests/agent/test_review_r3_notice_loss.py::test_legacy_quoted_notice_is_user_text[split-1]
All eight notice-loss probe variants fail on each mutant. Full raw commands and failures are in mutant-a.log and mutant-b.log. Neither collection/setup error nor timeout was counted as a killed mutant.

5. RED-before-fix confirmed independently

Command, cwd base tree:
  /opt/homebrew/bin/timeout 900 /tmp/rv-p0-tool-append-r4/base/.venv/bin/python -m pytest -q -p no:cacheprovider --tb=short --show-capture=no tests/agent/test_review_r3_notice_loss.py
Raw r3-red-before.log:
  8 failed in 0.45s
  EXIT: 1

6. Legacy byte-fidelity / trailing-notice hunt

test_review_r4_hunt.py generates 590 user payloads per shape from notice literals, complete and partial OPEN/CLOSE strings, adjacent pairs, ordinary text and Unicode/CRLF; all are wrapped by the actual format_steer_marker producer. Tested string and image-bearing list rows through split, actual pruning, re-pruning and both model-summary serializers. Separate grids append a genuine trailing notice after the same valid steers. These four grid tests pass at head. No user-byte loss from the final notice drop was reproduced on these valid producer-shaped inputs. The invariant is narrow: the outer CLOSE always follows every byte of such a steer, so a notice after that CLOSE is outside it. This is not proof that arbitrary corrupt/unwrapped legacy rows have trustworthy provenance.

Early new-hunt draft errors (list membership instead of flattened text; an image-free list for a pruning-fires assertion; leading-whitespace ambiguity in a draft fallback control) were corrected in the reviewer-only probe. Their failures are NOT findings. Final results are hunt-corrected.log and canonical-scoped.log, not the superseded draft logs. The minimal final fallback counterexample has no leading/trailing whitespace trick and passes list/recorded controls.

7. Recorded rows byte-identical to round 3

recorded_snapshot.py independently builds 30 recorded cases per revision using append_to_tool_row: string/list; ordinary/fake-marker output; notice/quoted-marker/Unicode user text; steer-notice-steer append order. Snapshots include row metadata/content, split output, both model serializers, protected positions, fallback and prune outputs. Head and base commands each exit 0; actual module paths point into the corresponding disposable tree.
  RECORDED_CASES: 30
  SHA256: b6790d211b111eac27e89ec4486cc1724607d4af1308250e74e1b5582c45feef
  RECORDED_BYTE_IDENTICAL: True
Artifacts: recorded-head.json, recorded-base.json, recorded-head.log, recorded-base.log. This differential supplements real DB-reload recorded controls; it is not an exhaustive universal proof.

8. Canonical per-file scoped run, through heavy, once

Command, cwd head tree, HERMES_TEST_WORKERS=2 HERMES_TEST_FILE_RETRIES=0 plus scratch HOME/TMP environment:
  /opt/homebrew/bin/timeout 900 /opt/homebrew/bin/heavy bash scripts/run_tests.sh tests/agent/test_durable_tool_append.py tests/agent/test_durable_tool_append_classes.py tests/agent/test_durable_tool_append_r3.py tests/agent/test_review_r3_notice_loss.py tests/agent/test_review_r3_hunt.py tests/agent/test_review_r2_boundaries.py tests/agent/test_review_r2_abort.py tests/agent/test_review_probes.py tests/agent/test_review_guards.py tests/agent/test_review_r4_hunt.py tests/agent/test_review_r4_fallback.py
Raw canonical-scoped.log:
  [heavy] waited 54s for a test slot
  === Summary: 11 files, 132 tests passed, 4 failed (100% complete) in 30.7s (2 workers) ===
  EXIT: 1

All four failures are R4.1: minimal string legacy case plus three notice-position variants. All builder files and archived probes pass. Per-file observed counts:
  test_durable_tool_append.py: 15
  test_durable_tool_append_classes.py: 32
  test_durable_tool_append_r3.py: 36
  test_review_r3_notice_loss.py: 8
  test_review_r3_hunt.py: 8
  test_review_r2_boundaries.py: 7
  test_review_r2_abort.py: 1
  test_review_probes.py: 8
  test_review_guards.py: 7
The byte test was deliberately run separately with REVIEW_SNAPSHOT, not through the env-cleaning wrapper.

9. Source/API checks

Compiled source in memory for agent/tool_row_append.py, agent/context_compressor.py and tests/agent/test_durable_tool_append_r3.py; compile.log EXIT: 0. No invented API: split_tool_message, format_steer_marker, fallback and serializer methods were imported and exercised; no API was added by this delta. Frozen diff whitespace check EXIT: 0. Lane reviewed-file git diff --exit-code EXIT: 0. No second authority introduced by this delta; the defect is guessing boundaries inside the existing authority.

Advisories / limitations, not additional blockers

- Missing .lane/preflight.md is a process observation; meaningful rework evidence exists. Not a second MUST-FIX in this final scoped round.
- Conservative retention of genuine interstitial system notices is accepted D2 behavior, not a defect. Records-only first-append legacy upgrade deviation D1 was not reopened.
- The final trailing-notice drop did not delete user bytes in the valid-producer grids; no fabricated truncated/corrupt history is offered as a blocker.
- No repository-wide suite, typechecker, build, live gateway, Sprite or weave-api DB run was performed. None was required by this latest scoped fork dispatch. No such result is claimed.

Artifacts and cleanup

All report, retained probes, drivers, snapshots and raw logs are under /tmp/rv-p0-tool-append-r4/.
Reviewed diff: reviewed.diff
Computed diff SHA256: e209a5de14fd97576b695215356f8fc216f66870d4f341c340ef28fc5dafa3c2

Each disposable tree's top-level path was verified before removal. git worktree remove --force for tree, base, mutant-a and mutant-b each exited 0. Readback in cleanup.log:
  TREE_EXISTS: False
  BASE_EXISTS: False
  MUTANT-A_EXISTS: False
  MUTANT-B_EXISTS: False
  REVIEW_REGISTRATIONS: []

No Sprite containers were created. All commands completed in foreground; no review server remains. No commits, pushes, PR comments, merges, installs, deploys, Linear updates or live-DB writes were made. Lane source was read-only throughout. Its pre-existing contributor-file modification was observed at start and end; no claim that the entire lane tree was clean is made.
