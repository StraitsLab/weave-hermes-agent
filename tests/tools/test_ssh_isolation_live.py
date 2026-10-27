"""Opt-in real-sshd proof. Caller supplies a disposable account and pinned host key."""
import os
from pathlib import Path

import pytest

from tools.environments.ssh import SSHEnvironment
from tools.environments.base import EnvironmentConnectionError

pytestmark = pytest.mark.skipif(not os.getenv('HERMES_ISOLATION_SSH_HOST'), reason='isolated SSH proof not configured')


def test_isolated_ssh_exec_persists_work_without_syncing_secrets(tmp_path, monkeypatch):
    from tools import env_passthrough
    from tools.environments import ssh
    home = tmp_path / 'agent-home'
    (home / 'skills').mkdir(parents=True)
    (home / 'skills' / 'agent-only.txt').write_text('agent-sentinel')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('ISOLATION_TEST_SECRET', 'must-stay-on-agent')
    monkeypatch.setattr(env_passthrough, 'get_all_passthrough', lambda: {'ISOLATION_TEST_SECRET'})
    # Any attempted automatic transfer fails the proof, including cleanup.
    monkeypatch.setattr(ssh, 'FileSyncManager', lambda **kw: pytest.fail('unexpected file sync'))
    known_hosts = tmp_path / 'known_hosts'
    known_hosts.write_text(os.environ['HERMES_ISOLATION_SSH_HOST_KEY'])
    args = dict(host=os.environ['HERMES_ISOLATION_SSH_HOST'],
                user=os.environ['HERMES_ISOLATION_SSH_USER'],
                port=int(os.environ['HERMES_ISOLATION_SSH_PORT']),
                key_path=os.environ['HERMES_ISOLATION_SSH_KEY'],
                known_hosts=str(known_hosts),
                cwd=os.environ['HERMES_ISOLATION_SSH_CWD'],
                sync_files=False, forward_env=False)
    env = SSHEnvironment(**args)
    try:
        result = env.execute("test -z \"${ISOLATION_TEST_SECRET:-}\" && printf '%s' isolated-ssh-ok", timeout=15)
        assert result['returncode'] == 0, result
        assert 'isolated-ssh-ok' in result['output']
        result = env.execute("mkdir -p native-proof && cd native-proof && printf '%s' survived > marker", timeout=15)
        assert result['returncode'] == 0, result
        result = env.execute('cat marker', timeout=15)
        assert result['returncode'] == 0 and 'survived' in result['output'], result
    finally:
        env.cleanup()
    # A new native environment can read the workspace from the previous one.
    env = SSHEnvironment(**args)
    try:
        result = env.execute('cat native-proof/marker', timeout=15)
        assert result['returncode'] == 0 and 'survived' in result['output'], result
    finally:
        env.cleanup()
    # Concurrent sessions share the computer, not each other's CWD or shell variables.
    from concurrent.futures import ThreadPoolExecutor
    environments = [SSHEnvironment(**args) for _ in range(4)]
    def work(item):
        index, environment = item
        result = environment.execute(f'mkdir -p session-{index} && cd session-{index} && export SESSION_MARKER={index}', timeout=15)
        assert result['returncode'] == 0, result
        result = environment.execute('printf "%s:%s" "$PWD" "$SESSION_MARKER"', timeout=15)
        assert result['returncode'] == 0, result
        assert f'/session-{index}:{index}' in result['output'], result
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(work, enumerate(environments)))
    finally:
        for environment in environments:
            environment.cleanup()
    assert (home / 'skills' / 'agent-only.txt').read_text() == 'agent-sentinel'
    assert list((home / 'skills').iterdir()) == [home / 'skills' / 'agent-only.txt']
    # Missing host trust refuses instead of learning a key or falling back locally.
    wrong = tmp_path / 'empty-known-hosts'
    wrong.write_text('')
    with pytest.raises(EnvironmentConnectionError):
        SSHEnvironment(**(args | {'known_hosts': str(wrong)}))
