# Ruling r5 — P0-TOOL-APPEND (lead, final)
Source: review-gate-r4.md (GPT BLOCK at d62c2d86, one P2 MUST-FIX R4.1; ESCALATE to lead).
Fact: on a record-less legacy row, a user steer that QUOTES an adjacent CLOSE/OPEN pair is byte-identical to two genuine
successive legacy steers. Provenance cannot be proven from the bytes, so no consumer may treat inner markers as
producer-owned.
Decision (fail-safe over-retention, class-wide): a record-less legacy region that contains more than one steer block is
ATOMIC for every consumer. Unwrap only its outermost producer OPEN (first) and CLOSE (last); every inner byte, markers
included, is kept verbatim as ONE piece. This applies to the split (_peel_string_by_markers :329-338), steer_texts
(:444-451), pruning/re-pruning, and the deterministic fallback in context_compressor.py (:4669-4673, :4807-4810,
:8373-8392). Recorded rows (tool_appends metadata present) are unchanged: they keep exact per-steer splitting.
- Consequence accepted by the lead: genuine successive legacy appends (A1, B2) on record-less rows are no longer split;
  they stay together as one piece whose text shows the inner markers. Legacy rows predate the feature; over-retention
  is the safe side. Rewrite the builder guard test_successive_legacy_appends_with_notice accordingly and record it as
  deviation D3 in .lane/deviations.md.
- r4's notice rule still holds (a notice is never deleted from inside a region).
Acceptance: reviewer probes .lane/reviewer-probes-r4/test_review_r4_fallback.py and test_review_r4_hunt.py UNCHANGED
pass (fallback fails at d62c2d86); r3 notice-loss 8/8, r3 hunt 8/8 and all r2 probes/byte snapshot stay green. If a
reviewer probe asserts the genuine-A1/B2 split this ruling retires, report it by name; do not edit it.
Mutants: (a) record-less multi-block region split per inner marker again; (b) fallback unwraps inner markers.
