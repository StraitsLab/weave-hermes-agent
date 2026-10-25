"""Keep platform-script custody tests independent of host mount permissions."""
import os
from pathlib import Path


def trust_host_ancestors(monkeypatch, sandbox):
    """Normalize only ancestors outside the sandbox; test-owned paths stay real.

    External macOS volumes can be group-writable. Their host policy is not the
    subject of these tests: wrong-owner/writable ancestors inside sandbox are.
    """
    import cron.harso_scripts as scripts

    ancestors = set(sandbox.resolve().parents)
    real_lstat = scripts.os.lstat

    def lstat(path, *args, **kwargs):
        st = real_lstat(path, *args, **kwargs)
        if Path(path) in ancestors:
            values = list(st)
            values[0] &= ~0o022
            values[4] = os.getuid()  # windows-footgun: ok
            return os.stat_result(values)
        return st

    monkeypatch.setattr(scripts.os, "lstat", lstat)
