# Isolated SSH computers

Use the native SSH terminal backend when user commands must run on a separate computer.
A remote browser address alone does not move Python execution away from the agent.

```yaml
terminal:
  backend: ssh
  ssh_host: computer.example
  ssh_user: computer
  ssh_port: 22
  ssh_key: /etc/computer/id_ed25519
  ssh_known_hosts: /etc/computer/known_hosts
  ssh_sync_files: false
  ssh_forward_env: false
  cwd: /workspace
```

`ssh_sync_files: false` disables automatic transfer of skills, credentials, and cache in both directions.
It uses the existing `SSHEnvironment` switch rather than a second execution backend.

`ssh_forward_env: false` prevents implicit skill/config environment forwarding. The SSH client ignores
ambient SSH configuration, disables agent forwarding and port forwarding, and uses explicit identity
files only. Set a direct host and explicit key, not a host alias from `~/.ssh/config`.
Secrets deliberately included in command text are not filtered by these settings.

A nonempty `ssh_known_hosts` requires an absolute path to a provisioned host-key file. Missing files
and unknown hosts refuse connection. Obtain the public key through an authenticated management
channel. Do not trust an unauthenticated network key scan. The connection-pool identity includes the
trust file contents so a new environment cannot reuse a connection authenticated against an old pin.

Defaults preserve existing behavior: automatic file sync and environment forwarding are enabled,
and an empty host-key path uses `accept-new`. Boolean transfer settings reject malformed values.
All settings use the existing profile-scoped terminal configuration. Recreate existing SSH environments
after a policy change. This does not implement remote browser Python, Vault transport, network ACLs,
or isolation for other locally executing tools.

## Verification

Run `tests/tools/test_ssh_isolation.py` for configuration projection and transfer boundaries.
Run `tests/tools/test_ssh_isolation_live.py` against a disposable SSH account for real execution.
The opt-in live test requires these environment variables:

- `HERMES_ISOLATION_SSH_HOST`
- `HERMES_ISOLATION_SSH_USER`
- `HERMES_ISOLATION_SSH_PORT`
- `HERMES_ISOLATION_SSH_KEY` (path only)
- `HERMES_ISOLATION_SSH_HOST_KEY` (public known-hosts line)
- `HERMES_ISOLATION_SSH_CWD` (test workspace)

The live test checks four concurrent sessions, current-directory isolation, file persistence across a new
connection, no automatic file sync, no skill-secret forwarding, and rejection of an untrusted host.
It creates `native-proof/marker` in the supplied test workspace. It does not reboot a server.
