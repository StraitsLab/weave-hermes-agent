"""Browser Python must run only on the configured isolated computer."""
import json
from unittest.mock import Mock

import pytest
from tools import browser_use_cli as browser


@pytest.fixture
def remote(monkeypatch):
    cfg = {'execution_backend': 'ssh', 'ssh_python': '/opt/browser/bin/python',
           'ssh_workspace': '/workspace/browser'}
    monkeypatch.setattr(browser, '_read_browser_cfg', lambda: cfg)
    local = Mock(side_effect=AssertionError('local Python must never execute'))
    monkeypatch.setattr(browser, '_find_cli', local)
    monkeypatch.setattr(browser, '_base_subprocess_env', local)
    monkeypatch.setattr(browser, '_attach_vault_supervisor', local)
    monkeypatch.setattr(browser, '_find_screenshot', local)
    terminal = {'env_type': 'ssh', 'ssh_host': 'computer', 'ssh_user': 'computer',
                'ssh_key': '/etc/pair/key', 'ssh_port': 22, 'ssh_sync_files': False,
                'ssh_forward_env': False, 'ssh_known_hosts': '/etc/pair/known_hosts'}
    monkeypatch.setattr('tools.terminal_tool._get_env_config', lambda: terminal)
    env = Mock()
    env.execute.return_value = {'returncode': 0, 'output': 'remote-result'}
    build = Mock(return_value=env)
    monkeypatch.setattr('tools.terminal_tool_backends._build_ssh_env', build)
    return cfg, terminal, env, build


def test_python_only_reaches_remote_stdin(remote):
    _cfg, _terminal, env, _build = remote
    code = 'print("untrusted Python")'
    result = json.loads(browser.browser_exec(code, task_id='session-a'))
    assert result['success'] and result['output'] == 'remote-result'
    assert result['execution_backend'] == 'ssh'
    command = env.execute.call_args.args[0]
    assert '-m browser_harness.run' in command
    assert code not in command
    assert code in env.execute.call_args.kwargs['stdin_data']
    assert 'env -i' in command and 'BU_AUTOSPAWN=0' in command
    assert 'VAULT' not in command
    env.cleanup.assert_called_once()


@pytest.mark.parametrize('key,value', [('env_type','local'), ('ssh_sync_files',True),
    ('ssh_forward_env',True), ('ssh_known_hosts',''), ('ssh_key','')])
def test_unsafe_terminal_policy_refuses_before_connection(remote, key, value):
    _cfg, terminal, _env, build = remote
    terminal[key] = value
    result = json.loads(browser.browser_exec('print(1)'))
    assert 'error' in result
    build.assert_not_called()


def test_remote_failure_never_retries_locally(remote):
    _cfg, _terminal, env, _build = remote
    env.execute.side_effect = RuntimeError('computer unavailable')
    result = json.loads(browser.browser_exec('print(1)'))
    assert 'error' in result
    env.cleanup.assert_called_once()


def test_sessions_get_separate_remote_workspaces(remote):
    first = json.loads(browser.browser_exec('print(1)', task_id='a', session='same'))
    second = json.loads(browser.browser_exec('print(1)', task_id='b', session='same'))
    assert first['workspace'] != second['workspace']


def test_local_override_refuses_instead_of_breaking_isolation(remote):
    result = json.loads(browser.browser_exec('print(1)', local=True))
    assert 'error' in result
    remote[3].assert_not_called()


def test_vault_is_explicitly_unavailable_until_remote_supervision(remote, monkeypatch):
    from tools import browser_vault_tool as vault
    fill = Mock(side_effect=AssertionError('must not expose local vault'))
    monkeypatch.setattr(vault, 'browser_vault_list', fill)
    result = json.loads(vault._handle_vault({'action':'list'}))
    assert not result['success'] and 'remote' in result['error'].lower()
    fill.assert_not_called()
