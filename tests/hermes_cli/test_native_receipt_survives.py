"""A native code update must not discard its in-flight receipt."""
import json
import subprocess
import sys
from pathlib import Path


def test_active_receipt_survives_stale_module_purge(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script = r'''
import os, sys, json
os.environ['HERMES_HOME'] = sys.argv[1]
from hermes_cli import main
from hermes_cli import update_receipt
from hermes_cli import update_cmd
update_receipt.begin_update_receipt()
original = update_receipt._current
assert original is not None
update_cmd._purge_stale_hermes_modules()
from hermes_cli.update_receipt import finalize_update_receipt
assert sys.modules['hermes_cli.update_receipt']._current is original
path = finalize_update_receipt('success')
assert path is not None and path.is_file()
assert json.loads(path.read_text())['outcome'] == 'success'
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)], cwd=root,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
