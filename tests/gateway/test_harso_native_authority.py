"""Exercise the real HTTP-to-gateway-to-provider path in an isolated process."""
import json
import os
from pathlib import Path
import subprocess
import sys


def test_harso_submit_executes_once_on_upstream_gateway(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    home, state = tmp_path / 'home', tmp_path / 'state'
    home.mkdir()
    state.mkdir()
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'TZ', 'SYSTEMROOT') if key in os.environ}
    env.update(HOME=str(home), USERPROFILE=str(home), HERMES_HOME=str(state), PYTHONPATH=str(repo))
    result = subprocess.run([sys.executable, str(Path(__file__).parent / 'fixtures' / 'harso_native_peer.py')],
        cwd=repo, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=65, check=False)
    assert result.returncode == 0, result.stdout + '\n' + result.stderr
    receipt = json.loads((state / 'receipt.json').read_text())
    assert receipt['rows'] == 1 and receipt['requests'] == 2
    assert receipt['internal_typed'] is True
