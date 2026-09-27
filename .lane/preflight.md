# Preflight — P0-TOOL-APPEND round 5 (fix of review r4): read this first
Base (blocked head) d62c2d86ee8ddbbbacb440a35de5bfdb0eb93b15. Frozen head 4fabdd66045ee24dfda768cf5b98858b390d1217. Fixer: Claude Opus; scope = the named findings only (rapid fix -> delta re-review).
Mutant tables and full suites are NOT in the fixer's scope; the lead runs the full suites in parallel with this review.

## Fix
Finding R4.1 (P2, pre-existing): record-less legacy region split at a user-quoted adjacent CLOSE/OPEN; fallback unwrapped both.
Fix: agent/tool_row_append.py _peel_string_by_markers (~:319-330): a record-less legacy region from first OPEN to final CLOSE is ONE
piece; the inner-split loop is removed. steer_texts/fallback/prune/serializers read that split unchanged. Recorded rows, legacy list
rows, the r4 notice rule, trailing-notice drop and the strict P0 xfail are unchanged. Deviation D3 (.lane/deviations.md): genuine
successive legacy appends are no longer split (lead ruling r5, fail-safe over-retention).

## Receipts (fixer-reported, lead verified head == origin)
- r4 fallback + hunt probes (byte-identical): at d62c2d86 `4 failed, 10 passed`; at fix `14 passed`.
- builder files + all r2/r3/r4 probes at fix: `143 passed`; test_review_bytes with REVIEW_SNAPSHOT: 1 passed, sha d79ed7c3... = archive.
- No unchanged probe asserts the retired A1/B2 split. Scoped selection: .lane/scoped-selection.txt (145 paths).
