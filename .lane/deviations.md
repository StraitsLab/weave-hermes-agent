# K-FORK-CLIENT deviations (design C / build plan P9 vs this branch)

1. P0 helper stubbed. `append_to_tool_row(agent, messages, part, *, kind)` does not exist on base 84ae89c383
   (P0 branch codex/p0-durable-tool-append had no commits when K froze). `agent/memory_delivery.py`
   `_resolve_append_helper()` looks it up on `agent.agent_runtime_helpers` at call time; absent -> no late fetch,
   no append (design C §10 "durable write fails -> block not appended; server keeps it pending"). Tests install a
   stub with P0's kind=memory contract (`api_content` only). Nothing to change when P0 lands except the item below.
2. Tool-row sidecar is not sent on this base. Send (`agent/conversation_loop.py:2392-2396`), `substitute_api_content`
   (`agent/turn_context.py:190`), token shadow (`agent/model_metadata.py:3753`) and reload
   (`hermes_state.py:13548`) substitute `api_content` for user/assistant only; P0's table owns "add tool".
   Pinned by a STRICT xfail `test_mid_turn_block_reaches_the_wire_once_p0_lands` — it fails loudly the moment P0
   merges so the marker is removed then. Until then the switch stays off (it is off by default) — P10 flips it.
3. Typed (list) tool content: mid-turn delivery is skipped for list-content tool rows (`deliver_mid_turn_memory`)
   because list sidecars (`api_content_json`) are P0 storage. Server keeps the item pending.
4. Wire contract names not yet in weave-cloud: J-COPILOT / D-SLOTS had no commits for the late-fetch endpoint,
   tool endpoint, `delivery` response object or `visible_seqs`/`memory_deliveries` when K froze. Client uses:
   context request `visible_seqs: [int]`; context response `delivery: {seq, lines:[{op, handle, text}]}`;
   turn post `memory_deliveries: [{seq, native_item_ref}]` (design C §5.2 names); late fetch
   `POST /internal/harso/late-deliveries` {scope, visible_seqs} -> {delivery}; tool
   `POST /internal/harso/memory-tool` {scope, action, query?, ref?}. Both paths are runtime config
   (`plugins.harso.late_fetch_path` / `tool_path`, validated `/internal/harso/...`), so the lead can match J's
   final routes without a code change. Late-fetch timeout `plugins.harso.late_fetch_timeout` (default 0.3 s).
5. `session_search` off (C §11/T24): done at tool-surface build (`inject_memory_provider_tools`) and only when
   `harso_memory` actually reached the surface. The "after the P10 backend health check" half is P10's flip
   procedure (the switch is the only gate on the cell side); not built here.
6. "Consolidated brief first after compaction" (T11 first half) is server-side (the copilot decides what to
   re-deliver from `visible_seqs`); the cell side is: strip deliveries from summarizer and `on_pre_compress` input,
   and report `visible_seqs` (which drop after compaction) so the server re-delivers. Tested at the cell boundary.
7. Wording: brief says fenced `<system-memory>` block; design C §4.1 and source use the existing `<memory-context>`
   fence. Kept `<memory-context>` (code at source wins; the sanitizer table and StreamingContextScrubber already
   know that tag) with the §3 header wording in the note line and in the delivery header.
8. (review r1 F4, lead ruling) **Hot switch vs tool surface: the switch reaches the TOOL SURFACE at the next
   tool-surface refresh boundary, not per call.** Rendering (turn-start delivery, fence note, visible_seqs,
   late fetch, acks, compaction strip) still reads `plugins.harso.copilot_enabled` per call. The tool surface
   (`harso_memory` advertised + routed, `session_search` displaced) is latched per provider instance
   (`HarsoMemoryProvider._tools_on`, set at construction) and re-read ONLY in `inject_memory_provider_tools`,
   which first calls `MemoryManager.refresh_tool_routing()` (re-indexes the existing `_tool_to_provider` table for
   a provider whose latch changed; no second dispatcher), then reconciles the agent's `tools`/`valid_tool_names`
   against that same index. Boundaries that call it: agent construction (`agent_init.py:1990`; the gateway builds a
   fresh agent per message, so there it takes effect on the next message) and ACP's explicit tool-surface rebuild
   (`acp_adapter/server.py:1255`, which already invalidates the system prompt). `refresh_agent_mcp_tools` keeps the
   CURRENT latch (it does not re-read the switch) and preserves the displacement (`reconcile_displaced_tools`).
   Why not per call: AGENTS.md "Prompt Caching Must Not Break — do not change toolsets mid-conversation"; flipping
   the advertised tools inside a live conversation breaks the cached prefix. Between boundaries the surface AND
   routing both stay as they were: OFF-latched -> no `harso_memory`, `session_search` present; ON-latched ->
   `harso_memory` advertised AND routed (the provider honours its latch, not the live switch), so it is never
   advertised-but-unroutable. On disable at a boundary `harso_memory` leaves surface and routing together and
   `session_search` is restored at its original position. ACP `/tools` (a read-only listing) passes
   `refresh_routing=False` so listing can never move the live agent's routing.

9. r3 turn-start in-flight OFF: returns the hint only (withholds legacy items too), not "legacy items + hint" as ruling-r3 item 2 says. Reason: the reviewer r2 probe, which must pass unchanged, asserts the in-flight item sentinel is not rendered; the request was sent copilot-shaped. Start-OFF turns are unchanged (byte-identical, sha 697069a5…). See rework-r3.md.
