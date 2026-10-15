# Harso browser compatibility on v0.22.1

Upstream owns the vault store, login classifier, backend unlock, secret redaction,
and Bot Desktop implementation. Do not restore the old fork copies.

Retained differences:
- `HERMES_BD_STATE_DIR` selects the attempt's short private state directory (Unix
  socket length); its browser jar is `<state>/profile`. Without the override,
  upstream's profile path and relative-path rules remain unchanged.
- The dock uses `AGENT_BROWSER_ARGS` like agent-browser. Chrome's independent
  password save/autofill is disabled before local launches, including no-screen
  sessions, without changing unrelated preferences.
- Web Bot Auth is opt-in via literal `browser.web_bot_auth: true`. A trusted
  plugin provides `browser_request_signer` (upstream accepts extension hook names).
  Signing is authority-bound, bounded, and fail-open unsigned. The registry
  reconciles live enable/disable and retries transient Fetch failures. The dialog
  bridge remains a separate exact-origin interceptor.
- `hermes computer-use screen bridge` serves the loopback `/display` and `/control`
  bridge used by the Harso live-view tunnel. The HMAC key is read from a private
  file, never argv or model context. Upstream's lease and RFB filter own takeover
  fencing and clipboard direction.
- Browser Use screenshot paths are redacted at every emitted text/meta boundary;
  only image loading retains the real path.

## Cell configuration (consumer-owned)

Keep exactly one vault authority: Harso's weave-api/browser computer. Cells must
exclude native `browser` / `browser-cdp` / `browser-use` toolsets where the Harso
browser computer supplies them, and must not select the five native
`browser_vault_*` tools. Upstream local vault has no global `vault.enabled` switch:
setting that old fork key to false does **not** disable it.

Also set `vault.onepassword.enabled: false`, `vault.bitwarden.enabled: false`,
`tools.connectors.enabled: false`, `nous.guest: false`, and exclude the native
`connections` toolset. These consumer settings belong to S6, not this patch.

Native vault fills arm their document before writing; if the guard plugin is not
active, the fill fails closed. Enable `harso-vault-guard` in `plugins.enabled` for
native-vault users. Its upstream `tool_execution` middleware refuses browser code,
vision, console expressions and page-reading CDP while a filled document remains
reachable, including sibling tasks on a shared browser. `tool_request` middleware
only rewrites arguments in v0.22; it has no refusal contract. New documents disarm;
back/forward-cache restores re-arm. Unknown/disconnected documents remain armed
(conservative: closing a tab may require a process restart to release custody).

Harso's separate browser computer (`weave_api/vault_browser.py`) does not call the
native fill path; its API/BEM guard remains consumer-owned. This native guard does
not claim to fence another process's browser tools. Upstream already supplies the
concrete identifier-input guidance and no-password input-tool note dynamically in
`model_tools.py`; no duplicate schema logic was added.
