# Harso MCP and chat compatibility

The cell fork keeps these additions over upstream v0.22.1:

- Root-key-only `POST /v1/mcp/reload` (empty body supported) and `POST /v1/mcp/refresh` (`{"server":"weave-connectors"}`). A schema refresh only calls `tools/list` on the existing selected connection; it never discovers or reconnects unrelated servers. Failures return a credential-free 503. Profile-prefixed routes cannot invoke these controls.
- The existing turn prologue adopts content changes under identical tool names. Active-turn snapshots are not rewritten by the HTTP route. Agent toolset restrictions remain in force.
- Completed tool refusals reset the transport breaker; transport errors still count. Upstream's trust gating, snake/camel read-only annotation support, selective shutdown, registry overlays, tool search batching/stemming and deferred schema validation are retained.
- Exact bare deferred calls enter the existing tool_call pipeline only if present in the agent's scoped catalog. `tools.tool_search.always_eager` pins selected names without broadening toolset selection. `skills-read` lists/reads skills without the writer.
- Forwarding execution middleware receives an idempotent `on_execution_started(final_args)` observer. A refusal that does not invoke it emits no start.
- Registry result/exception egress scrubs registered vault values before truncation/logging, including escaped and URL-encoded forms; declared inline-image/CDP binary paths remain opaque.

## Native chat integration (S1)

`gateway.weave_chat_sync.publish(session_id, ref, event, read_display, secret, *, on_finished=None)` preserves the old writer contract. The native session event emitter must feed it user-visible events, with terminal events after persistence. `read_display` returns committed display rows (including compacted history). `secret` reads the active profile's secrets. `hub_card(tool_name, raw_result)` extracts only approved hub answer cards; include that value as `card` on `tool.completed`. It supports upstream's deduplicated JSON TextContent result too.

This is deliberately not attached to upstream stream hooks: they are per-inference-attempt, have separate lossy queues for start/delta/end, omit native request refs and committed display rows, and do not signal durable turn completion. Using them as a turn writer would change row identity/finalization and race terminal delivery. The native event seam is the minimal correct integration point; native emitter files belong to S1.

The writer reads `WEAVE_CHAT_SUPABASE_URL`, `WEAVE_CHAT_SUPABASE_KEY`, and `WEAVE_API_MCP_BEARER`. Without them it is inert. Tests use local HTTP RPC fixtures, never a customer database. No live Supabase acceptance is claimed.
