"""Real native dispatcher, HTTP custody and CLI child; model only is a fixture."""

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace


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


def test_projection_events_run_inside_profile_scope(monkeypatch):
    """A loop callback has no request scope; the projection must enter its profile's home itself."""
    import asyncio
    from contextlib import contextmanager
    from gateway import native_bot_admission

    seen = []

    @contextmanager
    def scope(profile):
        seen.append(("enter", profile))
        yield
        seen.append(("exit", profile))

    adapter = SimpleNamespace(_profile_scope=scope,
                              _native_submit_event=lambda ref, kind, **f: seen.append((kind, ref)))
    projection = native_bot_admission.Projection(adapter, "general", "s1", asyncio.new_event_loop())
    projection.ref = "ref-1"
    projection._event("ref-1", "message.start", {})
    assert seen == [("enter", "general"), ("turn.started", "ref-1"), ("exit", "general")]
