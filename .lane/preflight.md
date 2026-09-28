# Preflight: weave-hermes-agent #61, WEV-2108 slice 3b (native submit busy_mode)

Scope: `/api/sessions/{id}/submit` honours busy_mode queue | steer | interrupt for a busy native session.
No new scheduler, no new dependency; weave-api still sends queue until slice 3c.

Round-1 review (t_c792f214, BLOCK at b62cc65) and what changed:
- F1 accepted steer lost: the finalizer now takes the last leftover and closes the steer window in one locked step (`AIAgent._close_steer_window`); a later steer is refused and queued as its own turn. The next turn reopens it. A leftover steer that meets a queued sibling now leads that sibling's message instead of being dropped.
- F2 stale interrupt: after the compression await the interrupt re-checks the same active ref, the same live agent, and no active subagents; otherwise it only queues.
- F3 mode-blind idempotency: busy_mode is in the request fingerprint; queue keeps the message-only hash so pre-existing records still replay.
- F4 weak tests: HTTP interrupt test, exact turn.completed / turn.failed settlement, all 6 mode transitions, legacy replay, real finalizer + real AIAgent steer window, fenced interrupt (3 target changes).
- F6 attribution: contributors/emails/hermes@straitslab.com mapped.

Evidence at this head:
- submit + finalizer tests: 39 passed. Related gateway/agent suites (21 files): 221 passed (207 at b62cc65).
- Reviewer's own probe.py + integration.py: 12 passed (9 failed + 1 failed at b62cc65). Two probe assertions were adapted to the new contract: the probe drains via `_close_steer_window` (the finalizer's real call), and the sibling case accepts the steer text leading the queued message.
- Mutants (9, one at a time, source restored): all killed.

Round 2 (Fly delta review, GPT-6 Astra, BLOCK at 1b0dbfdc3) and what changed:
- R2-F1: closing the window inside steer() broke the CLI / gateway / TUI /steer callers, which read False as "empty". steer() keeps its old contract; a new AIAgent.steer_if_open() refuses after the finalizer closes the window, and only native submit uses it (and queues on refusal). Reviewer's CLI and gateway reproductions now pass.
- R2-F3: the fingerprint was ambiguous (a queue message could spell the steer/interrupt encoding). Now queue = bare sha256 hex (legacy replay kept), other modes = "<mode>:<sha256 hex>", which no bare digest can equal.
Evidence: reviewer test_delta_regressions.py 6/6 pass; related suites + round-1 probes 216 passed; 10 mutants, all killed.
