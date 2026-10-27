# Remote browser execution

`browser.execution_backend: ssh` runs the existing browser-harness Python module on the
configured terminal computer. It never executes model Python on the agent host and never
falls back to local execution when SSH fails.

```yaml
browser:
  execution_backend: ssh
  ssh_python: /opt/browser/bin/python
  ssh_workspace: /workspace/browser
```

Requires the isolated SSH settings from `isolated-ssh-computers.md`. The computer needs
browser-harness and Chromium on loopback port 9222. Session workspaces and harness daemon
names derive from the agent profile, chat session, and named browser session. The SSH
transport only receives explicit non-secret environment values and Python through stdin.
Cloud auto-spawn is disabled. Browser helper scripts and output remain on the computer.
Remote output is treated as text; a printed screenshot path is never opened on the agent.

## Current boundaries

This is an opt-in candidate, not a complete hosted browser product. Vault is explicitly
unavailable on the remote path; no local supervisor socket or bearer crosses to the computer.
Login, payment and other secret entry must wait for a safe remote supervisor contract.
Automatic screenshot delivery and daemon lifecycle integration are not implemented here.
A user can change code on their computer; never interpret its output as trusted agent code.
URLs are checked by the existing policy, but arbitrary Python requires network confinement
at the computer boundary, not a URL regex. No hosted account is enabled by this change.

## Evidence

The real-SSH browser test verified Python hostname `comp-test-01`, absence of an agent-only
file and environment sentinel, DOM mutation/readback, and workspace/tab continuity across
calls. The test passed again after restarting the computer container. This is not a deployed
Harso gateway or full-server restart test. The existing prototype is not clean-image proof.

Chromium initially accepted CDP but could not create renderer threads. systemd's default
TasksMax was 76 inside the 512-task container. Setting the Chromium service TasksMax to
infinity restored rendering while retaining Incus pids.max=512. Apply that service setting
in the clean computer image, not by raising the whole host or container limit.
