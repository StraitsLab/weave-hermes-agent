"""Native SSH isolation policy, without a live SSH server."""
import os
from unittest.mock import Mock

import pytest

from tools.environments import ssh
from tools.terminal_scope import build_profile_terminal_scope, set_terminal_scope, reset_terminal_scope


def test_profile_ssh_policy_reaches_native_environment(tmp_path, monkeypatch):
    from tools.terminal_tool import _get_env_config
    from tools.terminal_tool_backends import _ssh_config_from_config, _build_ssh_env
    (tmp_path / "config.yaml").write_text(
        'terminal:\n  backend: ssh\n  ssh_host: computer\n  ssh_user: user\n'
        '  ssh_sync_files: false\n  ssh_forward_env: false\n  ssh_known_hosts: /etc/pair/known_hosts\n')
    token = set_terminal_scope(build_profile_terminal_scope(tmp_path))
    constructor = Mock()
    monkeypatch.setattr('tools.terminal_tool_backends._SSHEnvironment', constructor)
    try:
        config = _get_env_config()
        _build_ssh_env(cwd='/workspace', timeout=10, ssh_config=_ssh_config_from_config(config))
    finally:
        reset_terminal_scope(token)
    assert constructor.call_args.kwargs['sync_files'] is False
    assert constructor.call_args.kwargs['forward_env'] is False
    assert constructor.call_args.kwargs['known_hosts'] == '/etc/pair/known_hosts'


def isolated_env(monkeypatch, **kwargs):
    monkeypatch.setattr(ssh, '_ensure_ssh_available', lambda: None)
    monkeypatch.setattr(ssh.SSHEnvironment, '_establish_connection', lambda self: None)
    monkeypatch.setattr(ssh.SSHEnvironment, '_detect_remote_home', lambda self: '/workspace')
    monkeypatch.setattr(ssh.SSHEnvironment, 'init_session', lambda self: None)
    return ssh.SSHEnvironment('computer', 'user', **kwargs)


def test_no_sync_never_initializes_or_downloads_agent_files(monkeypatch):
    manager = Mock(side_effect=AssertionError('agent files must stay on agent'))
    monkeypatch.setattr(ssh, 'FileSyncManager', manager)
    env = isolated_env(monkeypatch, sync_files=False)
    env._before_execute()
    monkeypatch.setattr(env, '_control_sockets', list)
    env.cleanup()
    manager.assert_not_called()


def test_no_forward_does_not_resolve_skill_secrets(monkeypatch):
    resolver = Mock(side_effect=AssertionError('must not read secrets'))
    monkeypatch.setattr(ssh, 'resolve_passthrough_env', resolver)
    popen = Mock()
    monkeypatch.setattr(ssh, '_popen_bash', popen)
    env = isolated_env(monkeypatch, sync_files=False, forward_env=False)
    env._run_bash('pwd')
    resolver.assert_not_called()
    assert [arg for arg in popen.call_args.args[0] if arg.startswith('SendEnv=')] == ['SendEnv=-*']
    assert 'SendEnv=-*' in env._build_ssh_command()
    assert 'ForwardAgent=no' in env._build_ssh_command()


def test_pinned_host_is_strict_and_cannot_reuse_unpinned_master(monkeypatch, tmp_path):
    known_hosts = tmp_path / 'known_hosts'
    known_hosts.write_text('first host key')
    unpinned = isolated_env(monkeypatch, sync_files=False)
    pinned = isolated_env(monkeypatch, sync_files=False, known_hosts=str(known_hosts))
    command = pinned._build_ssh_command()
    assert 'StrictHostKeyChecking=yes' in command
    assert f'UserKnownHostsFile={known_hosts}' in command
    assert f'GlobalKnownHostsFile={os.devnull}' in command
    assert pinned.control_socket != unpinned.control_socket
    known_hosts.write_text('changed host key')
    changed = isolated_env(monkeypatch, sync_files=False, known_hosts=str(known_hosts))
    assert changed.control_socket != pinned.control_socket


def test_invalid_policy_fails_closed_before_connect(monkeypatch):
    connect = Mock()
    monkeypatch.setattr(ssh.SSHEnvironment, '_establish_connection', connect)
    with pytest.raises(ValueError):
        ssh.SSHEnvironment('computer', 'user', sync_files='false')
    connect.assert_not_called()


@pytest.mark.parametrize('key', ['SSH_SYNC_FILES', 'SSH_FORWARD_ENV'])
def test_malformed_transfer_policy_refuses_instead_of_enabling(key):
    from tools.terminal_tool import _get_env_config
    token = set_terminal_scope({'TERMINAL_' + key: 'tru'})
    try:
        with pytest.raises(ValueError, match='boolean'):
            _get_env_config()
    finally:
        reset_terminal_scope(token)
