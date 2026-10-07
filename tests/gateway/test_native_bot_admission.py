"""Real native dispatcher, HTTP custody and CLI child; model only is a fixture."""

import os
from pathlib import Path
import subprocess
import sys


def test_native_http_bot_roundtrip(tmp_path):
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            str(root / "tests/gateway/native_bot_smoke.py"),
            str(root),
            "--python",
            sys.executable,
        ],
        env=os.environ | {"TMPDIR": str(tmp_path)},
        text=True,
        capture_output=True,
        timeout=100,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"result": "passed"' in result.stdout
