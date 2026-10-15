# Cell configuration on v0.22.1

Write these overrides to **every** cell profile (including general/work and the
admin home if it ever starts Hermes). Do not change upstream core defaults.

```yaml
sessions:
  auto_prune: false
  auto_archive: false
curator:
  enabled: false
nous:
  guest: false
  keepalive_interval_seconds: 0
tools:
  connectors:
    enabled: false
vault:
  onepassword:
    enabled: false
  bitwarden:
    enabled: false
secrets:
  onepassword:
    enabled: false
  bitwarden:
    enabled: false
telemetry:
  shared_metrics:
    enabled: false
    send: false
updates:
  check: false
```

All keys above exist in `hermes_cli/config_defaults.py` at v0.22.1.

- `sessions.auto_prune` otherwise deletes ended sessions idle for 90 days;
  `auto_archive` soft-hides history (already false upstream).
- `curator.enabled: false` disables background skill maintenance, including
  recoverable moves into `skills/.archive/` after `archive_after_days: 30`.
  Setting an archive age to zero is **not** an off switch. No age override is
  needed while curator is disabled; explicit manual curator commands remain
  operator actions.
- `nous.guest: false` prevents creation/use of a free Nous identity when no
  provider is configured. `keepalive_interval_seconds: 0` disables auth keepalive.
  Explicitly select the cell's custom model route; do not configure a Nous
  fallback/auxiliary provider, Nous auth credentials, or `cron.provider: chronos`.
- `tools.connectors.enabled: false` disables native Nous connector discovery.
  Also add `connections` to `agent.disabled_toolsets` and omit it from all
  `platform_toolsets`; the existing connector MCP remains authoritative.
- `vault.*.enabled` prevents external password-manager discovery/unlock sources.
  Separate `secrets.*.enabled` keys prevent `op read` / Bitwarden Secrets Manager
  environment resolution (already false upstream). There is **no** upstream
  `vault.enabled` global switch: local browser vault is always available with
  the browser. These settings do not prove a single vault prompt owner; the
  cell's browser/vault tool ownership must be selected by its toolsets.
- `telemetry.shared_metrics.send` prevents metrics upload to Nous (already false).
  `updates.check` prevents passive version checks; explicit updates still work.
  These settings are not a general network denylist.

## Native installer caller

The existing flags remain accepted:

```sh
HERMES_HOME=/opt/hermes-admin UV_PYTHON_INSTALL_DIR=/opt/hermes-python \
HERMES_REPO_URL=https://github.com/StraitsLab/weave-hermes-agent.git \
bash /opt/hermes-admin/install.sh \
  --dir /opt/hermes/code --branch "$branch" --commit "$sha" --force-commit \
  --non-interactive --skip-setup --skip-browser --skip-computer-use --no-skills
```

`--force-commit` is compatibility-only: upstream `--commit` already enforces
branch ancestry and the exact checkout, stashing rather than discarding edits.
`--no-skills` writes upstream's persistent `.no-bundled-skills` marker **before**
product completion. Upstream still seeds essential `hermes-agent` skills; this
is the native opt-out contract, not a guarantee of zero skill files.

The v0.22 source installation is PM-managed. Consumers must use the published
`/opt/hermes/code/.hermes/bin/hermes` launcher, **not** the retired
`/opt/hermes/code/venv/bin/python` + `/opt/hermes/code/hermes` layout.

## Approval adapter contract

The native submit adapter should use upstream `register_gateway_notify()` /
`unregister_gateway_notify()` around each live turn and set `HERMES_EXEC_ASK=1`.
A registered API notifier is the attended surface; no fork-wide attended-session
set is needed. With no notifier, API/webhook decisions use `unattended_mode`
immediately, even if the process inherited CLI interactivity. Keep
`approvals.unattended_mode: deny`; do not auto-approve to avoid a wait.
