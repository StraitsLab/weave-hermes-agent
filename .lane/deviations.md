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
