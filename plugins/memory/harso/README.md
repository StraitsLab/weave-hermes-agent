# Harso memory provider

Select with `memory.provider: harso`. The upstream memory plugin loader calls
`register(ctx)` and owns activation; no HTTP route or second plugin loader is added.
Credentials are resolved from the current profile's secret scope on each call.

The chat profile receives recall and the always-on profile. `plugins.harso` retains
`prefetch_timeout`, `prefetch_max_bytes`, `context_max_chars`, `tools_enabled`,
`profile_enabled`, and `profile_wait`. Provider tools propose changes; they do not
claim to have committed a long-term memory write.

The built-in memory tool remains upstream's `add|patch|remove` contract with an
explicit top-level target. Its bridge reports whole committed entries for patches;
Harso translates these into the service's `assert|correct|retract` mutation vocabulary.
A native write needs an operation ID and revision to receive a correlated service
acknowledgement. A native success is not a provider acknowledgement.

## Re-port exclusions

The v0.21 identity-epoch rebuild was an opt-in patch spanning gateway cache keys,
conversation restore and turn-context code. No active weave-cloud source/config
consumer of `agent.identity_epoch_rebuild` or `agent.engine_identity` was found
(the latter appears only in historical provenance). Do not reinstate those cross-core
paths from their old tests/golden prompt: v0.22 prompt assembly and session lifecycle
are authoritative. Harso's supplied SOUL.md remains the primary identity through
upstream's own loader. The optional neutral engine identity prompt setting is retained
at prompt assembly; it does not alter user-supplied SOUL text or Codex fallback.

Turn sync requires persisted message `_row_id` values. The caller must pass the
native `turn_id` to `MemoryManager.sync_all`; do not derive identity from content.
The memory-propose dispatch must pass the current `messages` slice to
`MemoryManager.handle_tool_call` so it can name the durable current user row.
