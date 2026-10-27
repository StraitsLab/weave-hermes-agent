"""Run the unchanged browser harness on an isolated SSH computer, never beside Hermes."""
import hashlib
import posixpath
import shlex


def browser_exec_ssh(code, session, timeout, task_id, local, config):
    from agent.redact import redact_sensitive_text
    from hermes_constants import get_hermes_home
    from tools.browser_use_cli import _OWN_TAB_PREAMBLE, _SESSION_RE
    from tools.registry import tool_error, tool_result
    from tools.terminal_tool import _get_env_config
    from tools.terminal_tool_backends import _build_ssh_env, _ssh_config_from_config

    if local:
        return tool_error('Local browser override is unavailable on an isolated SSH computer.')
    if session and not _SESSION_RE.fullmatch(session):
        return tool_error('Invalid browser session name.')
    terminal = _get_env_config()
    if (terminal['env_type'] != 'ssh' or terminal.get('ssh_sync_files') is not False
            or terminal.get('ssh_forward_env') is not False
            or not terminal.get('ssh_known_hosts') or not terminal.get('ssh_key')):
        return tool_error('Remote browser requires isolated SSH with no transfers and pinned host trust.')
    python = config.get('ssh_python', '/opt/browser/bin/python')
    root = config.get('ssh_workspace', '/workspace/browser')
    if not all(isinstance(p, str) and posixpath.isabs(p) for p in (python, root)):
        return tool_error('Remote browser Python and workspace must be absolute paths.')
    identity = hashlib.sha256(f'{get_hermes_home()}\0{task_id}\0{session}'.encode()).hexdigest()[:24]
    workspace = posixpath.join(root, identity)
    # No supervisor socket/token or agent environment crosses this boundary. Vault stays disabled.
    command = 'mkdir -p ' + shlex.quote(workspace) + ' && ' + shlex.join([
        'env', '-i', 'PATH=/usr/local/bin:/usr/bin:/bin', 'HOME=' + root,
        'DISPLAY=:1', 'BU_CDP_URL=http://127.0.0.1:9222', 'BU_AUTOSPAWN=0',
        'BU_NAME=' + identity, 'BH_AGENT_WORKSPACE=' + workspace,
        'DO_NOT_TRACK=1', 'ANONYMIZED_TELEMETRY=false', python, '-m', 'browser_harness.run'])
    env = None
    try:
        # Fresh environment avoids a stale terminal instance carrying the old transfer policy.
        # Native ControlMaster still reuses the authenticated connection during this call.
        env = _build_ssh_env(cwd=root, timeout=timeout, ssh_config=_ssh_config_from_config(terminal))
        result = env.execute(command, cwd='/', timeout=timeout,
                             stdin_data=_OWN_TAB_PREAMBLE + code, bounded_capture=True)
        return tool_result({'success': result['returncode'] == 0, 'exit_code': result['returncode'],
            'output': redact_sensitive_text(result.get('output', ''), force=True),
            'execution_backend': 'ssh', 'workspace': workspace, 'session': session})
    except Exception as exc:
        return tool_error('Remote browser failed: ' + redact_sensitive_text(str(exc), force=True))
    finally:
        if env is not None:
            env.cleanup()
