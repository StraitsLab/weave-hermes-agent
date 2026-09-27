# Preflight — K-FORK-CLIENT round 5 (fix of review r4): read this first
Base (blocked head) 1d89ba0a9334cce78df6a995850bd7ff586eba67. Frozen head 78c20e379ca5b3f7c6bd097ecea8785b0764c804. Fixer: Claude Opus; scope = the named findings only (rapid fix -> delta re-review).
Mutant tables and full suites are NOT in the fixer's scope; the lead runs the full suites in parallel with this review.

## Fix
Finding r4 (P2, pre-existing): later unstamped passes re-rendered the memory fence note from the live switch.
Fix: note decided once with the first-consumption gate. agent/turn_context.py:591 field ext_prefetch_note; :1606 set once on the
stamped path, used :1609, carried :1693; agent/conversation_loop.py:2015 read from turn context; :2395 unstamped path decides it with
the gate; :2401 live composition reuses the frozen note. MoA suffix per-call; sidecars and OFF-origin unchanged; no new config read.
Compression/rebuild: no separate composition site; it falls into the same live block (source reading, not a mutant).

## Receipts (fixer-reported, lead verified head == origin)
- r4 probe test_r4_reviewer_hunt.py (sha matches .lane/reviewer-probes-r4): at 1d89ba0a `2 failed, 6 passed` (switch_flip
  [True-delivery-True], [True-items-True]); at fix `8 passed`, every case bytes_equal=True.
- r3 cache boundary 8 passed; r3 hunt 10 passed; r2 boundary 13 passed; touched files 115 passed.
- Scoped selection for the lead's parallel run: .lane/scoped-selection.txt (1730 collected with probes).
