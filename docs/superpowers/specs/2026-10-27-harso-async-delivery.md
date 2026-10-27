# Harso asynchronous delivery

## Outcome
The reply ends the native turn. The phone returns to Send while helpers/processes keep
running. Their progress uses the existing chat-items writer; completion queues an internal
notification in the same session and the agent's follow-up uses the same native writer.

## Native mechanism (baseline citations before implementation)
- Native submits already carry authenticated session identity and lifecycle callbacks
  (`gateway/platforms/api_server_harso_sessions.py:617-708`,
  `gateway/native_session_lifecycle.py:2-27`). Declare history delivery there, exactly as
  session-addressed desktop chat does (`gateway/platforms/api_server.py:3370-3385`, #98619).
- Register the native consumer on the existing private API binding receipt
  (`gateway/session_api.py:149-265`), not on an ID prefix or ambient environment variable.
  Ordinary `/v1` and desktop API consumers retain their current contracts.
- Reuse `SessionAuthority.admit_automation` and `native_text_v1.automation`
  (`gateway/session_automation.py:345-487`). Extend its existing route validation to the
  private API binding for native consumers; do not create another queue or HTTP channel.
- Completion identities already come from `producer_identity` and durable producer claims
  (`gateway/session_automation.py:12-40`, `gateway/run_notifications.py:1233-1282`).
  Admit first, acknowledge producer second. Stable identities deduplicate retries.
- The authority waits behind the classic native writer lease before claiming
  (`gateway/session_authority.py:1066-1070`). A completion never steers or interrupts.
- Human submits retain their existing native queue/steer/interrupt path unchanged.
  A later human submit may precede a queued completion. Both wait behind the active turn;
  completion delivery never steers or interrupts it.
- Execute API automation through the native request lifecycle/writer using its admission ID
  as the stable native request ref. Preserve the internal display marker and session proof.
  Reuse the native submit terminal index for crash cleanup, and carry the persisted runtime
  lock into `api_execution` so fallback refusal and settlement validation stay native.

## Confirmation safety (#85957)
Never call `/submit`, `/v1/chat/completions`, or a plaintext approval/clarify resolver for a
completion. The authority restores `event.internal=True` from its trusted automation envelope
(`gateway/session_envelope.py:291-299`); `_handle_message` bypasses pending-reply intercepts
for internal events (`gateway/run_inbound.py:1279`). Native submit's spoken-answer behavior
(`api_server_harso_sessions.py:655-673`) is unchanged and not reachable by this path.
While a human gate holds the active turn, the completion remains queued behind that turn.

## Phone and background commands
The existing native terminal event clears the writing row and ends Stop mode. Keep helper
progress on the existing final answer's delegate_task step after the parent finishes.
Update that same row through the existing writer cadence, without adding a text row or
reopening the parent turn. Keep progress attributed to its original turn. Reuse the existing helper
sanitization and Supabase row publisher. Native capabilities enable terminal notification
stamping too; no changes to hub MCP terminal semantics (user cells disable local terminal).

## Durability and restart
Queued automation payloads and API binding receipts are SQLite durable. API recovery must
schedule queued native automation as well as API input. Lost acknowledgements replay the
same admission, not another answer. A crash after execution starts follows the authority's
existing unknown/unattended policy: do not blindly rerun an uncertain answer. Exactly-once
inference across arbitrary crashes is not promised. Process/helper execution itself retains
its upstream restart behavior; transient helper progress is best-effort, not a new ledger.

## Verification
Fail first: native capability -> detached delegate handle and terminal notify; native wake
routing; same-session FIFO ordering against a held classic turn; stable duplicate receipt;
internal marker and untouched approval/clarify; idle helper status; API recovery. Run existing
wake, notifications, delegate dispatch, native submit, and chat writer suites and code health.
No live services, deployment, phone UI claim, or merge is part of this change.

## Out of scope
New push transport, parallel queue, client polling, new dependencies, durable live progress,
resuming child execution after crash, hub background jobs, and app UI changes unless an
existing projection cannot express a non-writing helper status. Live phone end-to-end proof
requires a later authorized rollout and is explicitly not claimed by this branch.
