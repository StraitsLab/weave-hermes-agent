"""Fork contract for the Bot Screen port (tools/bot_desktop/UPSTREAM.md).

T-1a-1: every file UPSTREAM.md lists as byte-identical hashes to the sha256 recorded there.
T-1a-4: ``HERMES_BD_STATE_DIR`` is the one state-dir authority; the Xvnc launcher child derives its socket from it.
T-1a-7: the dock's Browser icon starts Chromium with a preset ``AGENT_BROWSER_ARGS``.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import pytest

from tools.bot_desktop import browser, runtime

_PKG = Path(runtime.__file__).resolve().parent
_ROW = re.compile(r"^\| `(?P<name>[^`]+)` \| byte-identical \| `(?P<sha>[0-9a-f]{64})` \|$", re.MULTILINE)


def test_byte_identical_files_match_the_hashes_recorded_in_upstream_md():
    rows = {m["name"]: m["sha"] for m in _ROW.finditer((_PKG / "UPSTREAM.md").read_text(encoding="utf-8"))}
    assert set(rows) == {"__init__.py", "lease.py", "rfb_filter.py", "resources.py", "thumbnail.py",
                         "launcher.sh", "wallpaper.png"}
    for name, sha in rows.items():
        assert hashlib.sha256((_PKG / name).read_bytes()).hexdigest() == sha, f"{name} drifted from upstream"


def test_state_dir_env_is_the_one_authority_for_the_screen_state(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_BD_STATE_DIR", raising=False)
    monkeypatch.delenv("AGENT_BROWSER_PROFILE", raising=False)
    assert runtime.state_dir() == runtime.get_hermes_home() / "bot-desktop"  # unset: upstream layout
    assert browser.profile_dir() == runtime.state_dir() / "browser-profile"

    monkeypatch.setenv("HERMES_BD_STATE_DIR", "/tmp/bd")  # no-tmp: ok — the attempt's rendered value, not created
    assert runtime.state_dir() == Path("/tmp/bd")  # no-tmp: ok
    assert browser.profile_dir() == Path("/tmp/bd/profile")  # no-tmp: ok
    assert len(str(runtime.state_dir() / "rfb.sock").encode()) < 108  # AF_UNIX sun_path


def test_launcher_child_gets_the_socket_under_the_state_dir(tmp_path, monkeypatch):
    sd = tmp_path / "bd"
    sd.mkdir()
    monkeypatch.setenv("HERMES_BD_STATE_DIR", str(sd))
    monkeypatch.setattr(runtime, "_ALLOC_LOCK", tmp_path / "alloc.lock")
    monkeypatch.setattr(runtime, "_X_LOCK_DIR", tmp_path / "xlocks")
    monkeypatch.setattr(runtime, "_X_UNIX_TABLE", tmp_path / "unix")
    monkeypatch.setattr(browser, "dock_launch", lambda: None)
    monkeypatch.setattr(runtime, "_create_time", lambda pid: None)
    seen: dict = {}

    class _Exited:
        pid, returncode = 4242, 1

        def poll(self):
            return 1

    def _popen(argv, env, **_kw):
        seen.update(env)
        return _Exited()

    monkeypatch.setattr(runtime.subprocess, "Popen", _popen)
    with pytest.raises(RuntimeError, match="launcher exited"):
        runtime._spawn_and_wait(sd, wait_seconds=1)
    assert seen["HERMES_BD_SOCKET"] == str(sd / "rfb.sock")
    assert seen["HERMES_BD_XAUTH"] == str(sd / "Xauthority")
    assert str(runtime.get_hermes_home()) not in seen["HERMES_BD_SOCKET"]


def test_dock_browser_starts_with_the_preset_agent_browser_args(monkeypatch):
    monkeypatch.setattr("tools.browser_tool._needs_chromium_sandbox_bypass", lambda: False)
    monkeypatch.setenv("AGENT_BROWSER_ARGS", "--no-sandbox, --disable-dev-shm-usage,--disk-cache-size=268435456")
    argv = browser.dock_argv("/opt/chrome", "/p/dir")
    assert argv[-3:] == ["--no-sandbox", "--disable-dev-shm-usage", "--disk-cache-size=268435456"]

    monkeypatch.delenv("AGENT_BROWSER_ARGS")
    assert "--no-sandbox" not in browser.dock_argv("/opt/chrome", "/p/dir")  # unset: upstream bypass probe


def test_launcher_script_is_executable():
    assert (_PKG / "launcher.sh").stat().st_mode & 0o111
    assert subprocess.run(["bash", "-n", str(_PKG / "launcher.sh")], check=False).returncode == 0
