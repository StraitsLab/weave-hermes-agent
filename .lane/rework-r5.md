# P0-TOOL-APPEND rework round 5 (fix base d62c2d86ee8ddbbbacb440a35de5bfdb0eb93b15)

Authority: .lane/ruling-r5.md. Finding: reviewer r4 R4.1 (deterministic fallback deleted a
user-quoted adjacent CLOSE/OPEN pair on a record-less legacy string row). Scope cut by the
lead: fix R4.1 only; mutants and the scoped suite are run by the lead (.lane/scoped-selection.txt).

## Fix

agent/tool_row_append.py `_peel_string_by_markers`: the record-less region
`body[start:]` is appended as ONE piece (`steers.insert(0, body[start:])`); the inner
split-at-CLOSE/OPEN loop is removed. `steer_texts` already unwraps only a piece's first OPEN
and last CLOSE, so for the single atomic piece that is the region's outermost producer
wrapper; fallback, prune/re-prune, serializers and salvage all consume the same split, so
every consumer sees the inner bytes verbatim. Recorded rows and list rows untouched. D3 in
.lane/deviations.md.

## Tests (tests/agent/test_durable_tool_append_r3.py)

- Rewritten (D3): TestR22LegacyKeepsMore::test_successive_legacy_appends_with_notice,
  TestR31LegacyNoticeKept::test_quoted_notice_after_earlier_steer.
- New TestR41LegacyRegionAtomic (6): split one piece + steer_texts keeps inner markers;
  static fallback keeps the quoted boundary [none, inner notice, trailing notice]; prune and
  re-prune keep A1+B2 whole; DESIGN GUARD recorded adjacent steers still split [string, list].

## Raw lines (logs in .lane/logs-r5/; HOME/HERMES_HOME/TMPDIR in scratch; outer timeout)

- Reviewer r4 probes, unchanged (sha256 == archive r4-probes), fallback + hunt:
  - base d62c2d86 (detached worktree): `4 failed, 10 passed in 16.86s`
    (test_fallback_preserves_one_user_quoted_boundary[string-False],
     test_static_fallback_quoted_adjacent_boundary[before-string|after-string|none-string]).
  - fix: `14 passed in 15.73s`.
- Builder r3 file at base source: `5 failed, 38 passed` (the 2 rewritten guards + 3 R4.1 cases;
  the inner-notice fallback case and recorded guards pass at base).
- Fix, builder files + all reviewer probes (r4 fallback/hunt, r3 notice_loss/hunt, r2
  boundaries/abort, probes, guards): `143 passed in 33.38s`.
- test_review_bytes.py with REVIEW_SNAPSHOT: `1 passed`, sha256
  d79ed7c3b4df3cab55a57e108b446dbd9169252ea510f627078b81f9f93e72fd (matches).
- No unchanged reviewer probe asserts the retired A1/B2 split (all r2/r3/r4 probes green).
- Strict P0 xfail marker untouched; reviewer probes not committed under tests/.
