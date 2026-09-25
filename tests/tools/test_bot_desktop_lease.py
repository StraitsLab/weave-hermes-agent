"""Bot Desktop invariants: the byte-level RFB input gate follows the lease, and computer_use refuses
every action (capture included) while a human holds the screen."""

from __future__ import annotations

import json

import pytest

from tools.bot_desktop import lease
from tools.bot_desktop.rfb_filter import RfbClientFilter

_HANDSHAKE = b"RFB 003.008\n" + b"\x01" + b"\x00"
_KEY = b"\x04\x01\x00\x00\x00\x00\x00\x61"          # KeyEvent 'a' down
# QEMU Extended KeyEvent (type 255, sub 0): what noVNC sends once Xvnc advertises the pseudo-encoding.
_QEMU_KEY = bytes([255, 0, 0, 1]) + (0x65).to_bytes(4, "big") + (0x12).to_bytes(4, "big")
_POINTER = b"\x05\x01\x00\x10\x00\x10"              # PointerEvent, button 1
_CUT = b"\x06\x00\x00\x00\x00\x00\x00\x02hi"        # ClientCutText "hi"
_FBUR = b"\x03\x00" + b"\x00" * 8                   # FramebufferUpdateRequest
_SETENC = b"\x02\x00\x00\x02" + b"\x00\x00\x00\x07" + b"\xff\xff\xff\x21"  # SetEncodings x2


@pytest.fixture(autouse=True)
def _fresh_lease():
    from tools.computer_use import tool

    tool.reset_backend_for_tests()
    lease._reset_for_tests()
    yield
    tool.reset_backend_for_tests()
    lease._reset_for_tests()


def test_rfb_filter_forwards_input_only_from_the_lease_holder_across_arbitrary_chunking():
    f = RfbClientFilter(lambda: lease.viewer_may_send_input("v1"))
    head = f.feed(_HANDSHAKE)
    assert head[-1:] == b"\x01", "ClientInit is forced shared so a viewer never kicks the agent's watcher"

    # Agent holds: read-only messages pass, input is dropped, even when split byte by byte.
    stream = _KEY + _FBUR + _POINTER + _SETENC + _CUT + _QEMU_KEY
    out = b"".join(f.feed(stream[i:i + 1]) for i in range(len(stream)))
    assert out == _FBUR + _SETENC

    lease.acquire("v1")
    assert f.feed(_KEY + _POINTER + _QEMU_KEY) == _KEY + _POINTER + _QEMU_KEY

    lease.acquire("v2")  # last writer wins: v1 is evicted from input on the very next message
    assert f.feed(_KEY) == b""
    assert lease.release("v1").holder == lease.HUMAN, "a stale viewer's release must not yank control from v2"
    assert lease.release("v2").holder == lease.AGENT


def test_lease_authority_is_shared_across_processes(tmp_path):
    """The gateway that streams the screen and the process running the agent are different processes;
    a human takeover in one must refuse actions in the other."""
    import os
    import subprocess
    import sys

    lease.acquire("desktop-viewer")
    probe = ("import sys; sys.path.insert(0, %r)\n"
             "from tools.bot_desktop import lease\n"
             "try:\n    lease.assert_agent_may_act(); print('AGENT')\n"
             "except lease.HumanHasControl:\n    print('HUMAN')\n"
             "lease.release('desktop-viewer')\n") % os.getcwd()
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, encoding="utf-8", timeout=30,
                         stdin=subprocess.DEVNULL, env={**os.environ, "HERMES_HOME": os.environ["HERMES_HOME"]})
    assert out.stdout.strip() == "HUMAN", out.stderr
    assert lease.get().holder == lease.AGENT, "the other process's release is visible here"


def test_unreadable_lease_file_fails_closed_and_takeover_keeps_the_agents_reason(tmp_path):
    """Missing file = fresh profile (agent). A file that exists but cannot be parsed must not read as
    "agent holds": a torn write must never let the agent act on a human's screen. Taking over after a
    request keeps the agent's reason so the human still sees WHY while they act."""
    from hermes_constants import hermes_home_key

    home = str(tmp_path)
    assert lease.get(profile_key=home).holder == lease.AGENT
    path = lease._path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ torn", encoding="utf-8")
    assert lease.get(profile_key=home).holder == lease.HUMAN
    lease.release(profile_key=home)  # a successful write repairs it
    assert lease.get(profile_key=home).holder == lease.AGENT

    # Valid JSON of the wrong shape is just as untrustworthy as torn JSON: never read it as "agent holds".
    for wrong_shape in ("[]", "null", "5", "{}", '{"holder": "root"}'):
        path.write_text(wrong_shape, encoding="utf-8")
        assert lease.get(profile_key=home).holder == lease.HUMAN, wrong_shape
    lease.release(profile_key=home)
    assert lease.get(profile_key=home).holder == lease.AGENT

    held = lease.acquire("desk-1", reason="log in to the bank, 2FA on your phone", profile_key=home)
    assert held.reason == "log in to the bank, 2FA on your phone"
    assert hermes_home_key(home)  # sanity: the key derivation used by the bridge is available


def test_lease_works_without_fcntl(tmp_path):
    """Windows and fcntl-less hosts: ``computer_use`` imports the lease on EVERY call, so a module-level
    fcntl dependency turns every desktop action into ModuleNotFoundError there. The file semantics must
    still work; only the cross-process lock degrades. Subprocess so the module cache is clean."""
    import os
    import subprocess
    import sys

    probe = ("import sys; sys.modules['fcntl'] = None; sys.path.insert(0, %r)\n"
             "from tools.bot_desktop import lease\n"
             "import tools.computer_use.tool\n"
             "assert lease.get().holder == lease.AGENT\n"
             "assert lease.acquire('v1').holder == lease.HUMAN\n"
             "assert lease.get().holder == lease.HUMAN\n"
             "assert lease.release('v1').holder == lease.AGENT\n"
             "print('OK')\n") % os.getcwd()
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, encoding="utf-8", timeout=60,
                         stdin=subprocess.DEVNULL, env={**os.environ, "HERMES_HOME": str(tmp_path)})
    assert out.stdout.strip() == "OK", out.stderr


@pytest.mark.linux_only
def test_lease_files_are_private_even_when_the_lease_is_written_before_the_screen_exists(tmp_path, monkeypatch):
    """A takeover can be recorded before start() ever created bot-desktop/ 0700. The lease path then created
    the directory and files with the umask (0755 / 0644): who holds the screen, and the lock the RFB bridge
    serialises on, readable and clobberable by every other local user. Every piece must be owner-only."""
    import os
    import stat

    home = tmp_path / "deep" / "home"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(lease, "get_hermes_home", lambda: home)
    old = os.umask(0o022)
    try:
        lease.acquire("v1")
    finally:
        os.umask(old)
    sd = home / "bot-desktop"
    for path in (sd, sd / "lease.json", sd / "lease.lock"):
        assert path.exists(), path
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, f"{path.name} is {oct(path.stat().st_mode)}"


def test_no_op_transitions_do_not_bump_the_epoch():
    """Callers void an admitted in-flight action when the epoch moved. A release on an agent-held lease
    (double-clicked Hand back, a stray CLI stop) or the same viewer re-acquiring changes nothing real, so
    it must not make a legitimate agent action look overtaken."""
    e0 = lease.get().epoch
    assert lease.release().epoch == e0
    got = lease.acquire("v1", reason="log in please")
    assert got.epoch == e0 + 1
    again = lease.acquire("v1")
    assert again.epoch == got.epoch and again.reason == "log in please" and again.since == got.since
    assert lease.acquire("v2").epoch == got.epoch + 1  # a different viewer IS a transition
