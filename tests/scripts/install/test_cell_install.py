"""Cell compatibility flags exercise real installer stages without downloads."""
import os
from pathlib import Path
import shlex
import subprocess

import pytest

pytestmark = pytest.mark.platforms("posix")
INSTALL = Path(__file__).resolve().parents[3] / "scripts/install.sh"


def test_cell_flags_pin_and_opt_out_before_products(tmp_path):
    home = tmp_path / "admin"
    code = tmp_path / "code"
    code.mkdir()
    # The real completion must see the opt-out before any skill synchronization.
    body = f'''
source {shlex.quote(str(INSTALL))} --manifest --dir {shlex.quote(str(code))} --branch cells --commit abc --force-commit --non-interactive --skip-setup --skip-browser --skip-computer-use --no-skills
[ "$BRANCH:$INSTALL_COMMIT:$NON_INTERACTIVE" = cells:abc:true ] || exit 10
bootstrap_python() {{ boot_py=python3; }}
run_logged() {{ test -f "$HERMES_HOME/.no-bundled-skills"; }}
wire_shell_path() {{ :; }}
stage_products
'''
    result = subprocess.run(["bash", "-c", body], env=dict(os.environ, HERMES_HOME=str(home)),
                            capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (home / ".no-bundled-skills").exists()


def test_unknown_flags_still_fail():
    result = subprocess.run(["bash", str(INSTALL), "--not-a-real-option"],
                            capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode != 0
    assert "unknown option" in result.stderr
