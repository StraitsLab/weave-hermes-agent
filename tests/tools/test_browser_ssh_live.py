"""Opt-in: real remote browser Python and Chromium, no model or credentials."""
import json
import os

import pytest

pytestmark = pytest.mark.skipif(not os.getenv('HERMES_ISOLATION_SSH_HOST'), reason='isolated SSH proof not configured')


def test_browser_python_and_page_run_on_computer(tmp_path, monkeypatch):
    from tools import browser_use_cli as browser
    from tools.terminal_scope import set_terminal_scope, reset_terminal_scope
    known = tmp_path / 'known_hosts'
    known.write_text(os.environ['HERMES_ISOLATION_SSH_HOST_KEY'])
    token = set_terminal_scope({
        'TERMINAL_ENV': 'ssh', 'TERMINAL_SSH_HOST': os.environ['HERMES_ISOLATION_SSH_HOST'],
        'TERMINAL_SSH_USER': os.environ['HERMES_ISOLATION_SSH_USER'],
        'TERMINAL_SSH_PORT': os.environ['HERMES_ISOLATION_SSH_PORT'],
        'TERMINAL_SSH_KEY': os.environ['HERMES_ISOLATION_SSH_KEY'],
        'TERMINAL_SSH_KNOWN_HOSTS': str(known),
        'TERMINAL_SSH_SYNC_FILES': 'false', 'TERMINAL_SSH_FORWARD_ENV': 'false'})
    monkeypatch.setattr(browser, '_read_browser_cfg', lambda: {
        'execution_backend': 'ssh', 'ssh_python': '/opt/browser-proof/bin/python',
        'ssh_workspace': '/home/workspace/paired-browser-proof'})
    monkeypatch.setenv('AGENT_ONLY_PROOF_SECRET', 'not-for-computer')
    sentinel = tmp_path / 'agent-only-file'
    sentinel.write_text('agent-only')
    code = f'''import os, pathlib, socket, json
assert os.environ.get("AGENT_ONLY_PROOF_SECRET") is None
assert not pathlib.Path({str(sentinel)!r}).exists()
assert socket.gethostname() == "comp-test-01"
new_tab("about:blank")
js("document.title = 'Paired browser proof'; document.body.innerHTML = '<button id=proof>Check</button>'")
js("document.querySelector('#proof').textContent = 'Passed'")
assert js("document.querySelector('#proof').textContent") == 'Passed'
assert js('document.title') == 'Paired browser proof'
pathlib.Path(os.environ['BH_AGENT_WORKSPACE'], 'proof.txt').write_text('browser-persisted')
print('REMOTE_BROWSER_PYTHON_OK')
'''
    try:
        result = json.loads(browser.browser_exec(code, session='liveproof', task_id='browser-proof', timeout_s=90))
        assert result['success'], result.get('output', str(result))
        assert 'REMOTE_BROWSER_PYTHON_OK' in result['output'], result
        result = json.loads(browser.browser_exec("import pathlib, os; print(pathlib.Path(os.environ['BH_AGENT_WORKSPACE'], 'proof.txt').read_text()); print(js('document.title'))", session='liveproof', task_id='browser-proof', timeout_s=90))
        assert result['success'], result.get('output', str(result))
        assert 'browser-persisted' in result['output'] and 'Paired browser proof' in result['output'], result
    finally:
        reset_terminal_scope(token)
