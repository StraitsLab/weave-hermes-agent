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

Required profile secrets: `WEAVE_API_URL` and `WEAVE_API_MCP_BEARER`. The chat
session comes from the native run context of the current turn; outside a Harso app
turn, `browse` is refused. Model routes remain
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

## Browser-card write

After each successful page-bearing browse, the plugin best-effort POSTs to
`/v1/browser/activity` using `WEAVE_API_URL` and the cell's `WEAVE_API_MCP_BEARER`.
The JSON body is `{session_id,target,url,title}`. The session comes from native
turn context, not model arguments or a stale environment value.
The request has a 3-second HTTP timeout, does not follow redirects, and never
changes the browser answer on a network/server failure or missing native context.

The matching weave-cloud route resolves the cell owner and session conversation,
refuses targets absent from that owner's BEM open pages, and uses live inventory
metadata for `browser_activity.touch`. Cards retain conversation + target
deduplication; `touch` does not use turn_id. No legacy command is replayed and no
caller-supplied owner, organization or conversation is accepted. Attempt/JeV
scoped-token callers are not supported by this cell-only seam.

## Focused verification

```sh
PATH=<venv>/bin:$PATH HERMES_PYTHON=<venv>/bin/python scripts/run_tests.sh -j 2 \
  tests/plugins/test_harso_jev.py tests/tools/test_harso_vault_guard.py
```

Tests use the real pinned Jev action loop with fake CDP/model responses, and test
the plugin's backend/worker wiring. They do not establish live browser-computer,
request-signing, Vault tab selection or takeover acceptance. Those are deployment
acceptance, not permission to roll out this branch.
