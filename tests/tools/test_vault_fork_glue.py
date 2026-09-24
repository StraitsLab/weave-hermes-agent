"""Fork glue for the vault port (upstream @49b4286a22): atomic_write_bytes, the approval ``title=``
pass-through and the ``_gateway_notify_cb`` accessor, and CDPSupervisor.focus_page's failure contract."""

from __future__ import annotations

import os
import stat

import pytest


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_atomic_write_bytes_forces_mode_and_replaces_whole(tmp_path):
    from utils import atomic_write_bytes

    target = tmp_path / "vault" / "vault.json.enc"
    atomic_write_bytes(target, b"first", mode=0o600, fsync_dir=True)
    assert target.read_bytes() == b"first"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    target.chmod(0o640)
    atomic_write_bytes(target, b"second")  # no mode: an existing target keeps its bits
    assert target.read_bytes() == b"second"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert [p.name for p in target.parent.iterdir()] == ["vault.json.enc"], "temp file leaked"


@pytest.mark.require_symlinks
def test_atomic_write_bytes_fsyncs_the_resolved_parent_of_a_symlinked_target(tmp_path, monkeypatch):
    """Upstream 01a7efaed5: the rename lands in the real file's directory, so that is the one to fsync.
    (Directory fsync is a no-op on native Windows, so there no directory is opened at all.)"""
    import utils

    real, links = tmp_path / "real", tmp_path / "links"
    real.mkdir()
    links.mkdir()
    (real / "blob").write_bytes(b"old")
    (links / "blob").symlink_to(real / "blob")
    opened = []
    real_open = os.open
    monkeypatch.setattr(utils.os, "open", lambda p, flags, *a: opened.append(p) or real_open(p, flags, *a))

    utils.atomic_write_bytes(links / "blob", b"new", mode=0o600, fsync_dir=True)

    assert (real / "blob").read_bytes() == b"new" and (links / "blob").is_symlink()
    dirs_opened = [p for p in opened if os.path.isdir(p)]  # mkstemp also os.open()s the temp file
    assert dirs_opened == ([] if os.name == "nt" else [str(real)])


def test_title_reaches_callbacks_that_accept_it_and_legacy_callbacks_still_work(monkeypatch):
    from tools import approval

    seen = {}

    def modern(command, description, *, allow_permanent=True, title=None):
        seen["modern"] = title
        return "once"

    def legacy(command, description, allow_permanent=True):
        seen["legacy"] = "called"
        return "once"

    assert approval.prompt_dangerous_approval("c", "d", approval_callback=modern, title="Confirm payment card fill?") == "once"
    assert approval.prompt_dangerous_approval("c", "d", approval_callback=legacy, title="Confirm payment card fill?") == "once"
    assert seen == {"modern": "Confirm payment card fill?", "legacy": "called"}


def test_elicitation_consent_forwards_title_to_the_cli_prompt(monkeypatch):
    from tools import approval

    captured = {}

    def fake_prompt(message, description, **kwargs):
        captured.update(kwargs)
        return "once"

    monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: False)
    monkeypatch.setattr(approval, "prompt_dangerous_approval", fake_prompt)
    assert approval.request_elicitation_consent("m", "d", surface="vault-payment", title="Confirm?") == "accept"
    assert captured["title"] == "Confirm?" and captured["allow_permanent"] is False


def test_gateway_notify_cb_accessor_tracks_registration():
    from tools import approval

    cb = object()
    approval.register_gateway_notify("vault-port-sess", cb)
    try:
        assert approval._gateway_notify_cb("vault-port-sess") is cb
    finally:
        approval.unregister_gateway_notify("vault-port-sess")
    assert approval._gateway_notify_cb("vault-port-sess") is None


def test_focus_page_refuses_without_a_running_loop():
    from tools.browser_supervisor import CDPSupervisor

    sup = CDPSupervisor.__new__(CDPSupervisor)
    sup._loop = None
    assert sup.focus_page("https://example.com") == {"ok": False, "error": "supervisor loop is not running"}
