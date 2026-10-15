"""Harso-only Bot Screen compatibility; upstream owns the desktop and vault."""
import argparse
import json
import stat


def test_short_state_dir_keeps_the_attempt_profile_and_socket_together(tmp_path, monkeypatch):
    from tools.bot_desktop import browser, runtime

    monkeypatch.setenv("HERMES_BD_STATE_DIR", str(tmp_path / "bd"))
    assert runtime.state_dir() == tmp_path / "bd"
    assert browser.profile_dir() == tmp_path / "bd" / "profile"
    monkeypatch.setenv("AGENT_BROWSER_PROFILE", str(tmp_path / "explicit"))
    assert browser.profile_dir() == tmp_path / "explicit"


def test_dock_uses_the_same_explicit_browser_flags(monkeypatch):
    from tools.bot_desktop import browser

    monkeypatch.setenv("AGENT_BROWSER_ARGS", "--no-sandbox,--disable-dev-shm-usage,--lang=en")
    assert browser.dock_argv("chrome", "/profile", sandbox_bypass=False)[-3:] == [
        "--no-sandbox", "--disable-dev-shm-usage", "--lang=en"]


def test_password_manager_hardening_preserves_other_preferences(tmp_path):
    from tools.bot_desktop.browser import disable_password_manager

    prefs = tmp_path / "Default" / "Preferences"
    prefs.parent.mkdir()
    prefs.write_text(json.dumps({"other": 1, "profile": {"other": 2}}))
    disable_password_manager(str(tmp_path))
    result = json.loads(prefs.read_text())
    assert result["other"] == 1 and result["profile"]["other"] == 2
    assert result["credentials_enable_service"] is False
    assert result["credentials_enable_autosignin"] is False
    assert result["profile"]["password_manager_enabled"] is False
    assert result["password_manager"]["password_manager_blocklist"] == ["*"]
    assert stat.S_IMODE(prefs.stat().st_mode) == 0o600


def test_bridge_cli_coexists_with_upstream_screen_actions(monkeypatch):
    from hermes_cli import live_view_bridge
    from hermes_cli.subcommands import computer_use_screen

    parser = argparse.ArgumentParser()
    computer_use_screen.build_screen_parser(parser.add_subparsers(dest="command"), lambda *a: None)
    args = parser.parse_args(["screen", "bridge", "--secret-file", "key", "--port-file", "port"])
    seen = []
    monkeypatch.setattr(live_view_bridge, "run_bridge", lambda *args: seen.append(args) or 0)
    assert args.screen_func(args) == 0
    assert seen == [("127.0.0.1:0", "key", "port")]
    assert parser.parse_args(["screen", "status"]).computer_use_screen_action == "status"
