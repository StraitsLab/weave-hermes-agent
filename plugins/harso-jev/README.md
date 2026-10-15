# Harso Jev

One bundled `browse` tool; opt-in, not enabled by this change. Enable `harso-jev`
in `plugins.enabled` and the `harso-jev` toolset in the serving profile. Remove the
weave-api MCP `browse` tool from that profile before switching so there is one owner.
No weave-api wrapper or deployment is changed here.

The plugin resolves **the same backend as `browser_exec`**, including
`BU_CDP_WS`/`BU_CDP_URL` and `BROWSER_CDP_URL`, then runs the pinned Jev package in
an isolated Python subprocess using Hermes's browser-harness 0.1.13 daemon. It
never launches/restarts Chrome or provisions another browser. Fresh calls create
an owned **new window** in that browser (`Target.createTarget` with `newWindow: true`);
`browser_page.target` resumes without navigating.
The harness's current tab is switched too, so subsequent native browser tools can
continue there. Tabs remain open; the worker detaches its own CDP session.

Required profile secrets/context: `WEAVE_API_URL`, `WEAVE_API_MCP_BEARER`, and
`WEAVE_HERMES_SESSION_ID` for cell/attempt bearers. Model routes remain
`/v1/jev/systemone` and `/v1/jev/text/chat/completions`; the session header is sent
only to the latter. Credentials enter the worker via stdin, not command-line args
or the harness daemon's environment. `TYPESAFE_MODEL` and `TEXT_MODEL` retain the
wrapper's defaults. No new model/provider credential source is introduced.

Safety retained: HTTPS start URL and explicit host allowlist (including its
subdomains), refusal before off-host observations/model calls, 10-tick budget,
60-second navigation deadline, 300-second process-group timeout, independent
`expect` check against the final live page, paged visible text, exact target
resumption, safe popup tracking, and native human-control lease/redaction hooks.
The host allowlist is an observation/action boundary, **not** a network firewall;
Chrome can issue navigation/subresource requests before an off-host page is refused.

A login form returns `NativeLoginRequired` and its target. Use Hermes's native
`browser_vault_*` tools; no `use_vault`, `vault_used`, secret argv, file-based Vault
bridge, auto-submit or Sprite Chrome restart exists here. The Harso vault guard
now blocks `browse` while a vault-filled document is reachable. Complete the login
with native page-blind tools; resume Jev only after a new document clears custody.
Existing user approvals and restrictions still apply; do not supply secrets in goals.

## Browser-card write: server prerequisite (not implemented)

The existing server has no native page-activity MCP tool/route.
`ledger_mcp.bem_sprite_mcp` lines 757–769 writes the card only as a side effect of
executing a legacy remote command. `page_read`, `page_transfer`, and the live-view
routes cannot accept `{target,url,title}` and are not activity-write APIs. Calling
legacy `terminal` just to manufacture this side effect would re-run commands on
the wrong engine and defeat the port. Thus this branch returns `browser_page` but
**does not yet create the app's chat card row**.

Minimal proposed server addition: `POST /v1/browser/activity`, with cell bearer in
`Authorization`, `Weave-Hermes-Session` header, and JSON
`{"page":{"target":"<32 uppercase hex>","url":"https://…","title":"…"}}`.
Server-side only: resolve bearer owner and session conversation using the same
`resolve_cell_bem_owner` / `resolve_cell_session_conversation` bindings as
`bem_sprite_mcp`; verify the page target belongs to that owner's browser computer
(using the existing target inventory check); validate/bound page fields; call
`browser_activity.touch` with the bound organization/user/conversation/subject.
No caller-supplied owner, organization or conversation. `touch` already deduplicates
by conversation + target and currently does not use turn_id. Return an explicit
ack/error. Restrict this first version to the cell bearer; attempt/JeV scoped-token
support requires an explicit matching binding, not relaxed auth. Once available,
the plugin needs one bounded best-effort POST after each page-bearing result,
without changing the browser result on network failure. Until then card continuity
is a known rollout blocker; no fake success flag or nonexistent endpoint call.

## Focused verification

```sh
PATH=<venv>/bin:$PATH HERMES_PYTHON=<venv>/bin/python scripts/run_tests.sh -j 2 \
  tests/plugins/test_harso_jev.py tests/tools/test_harso_vault_guard.py
```

Tests use the real pinned Jev action loop with fake CDP/model responses, and test
the plugin's backend/worker wiring. They do not establish live browser-computer,
request-signing, Vault tab selection or takeover acceptance. Those are deployment
acceptance, not permission to roll out this branch.
