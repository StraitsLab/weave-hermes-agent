"""cli.py's startup bridge must share the home-mode table (WEV-1330).

``cli.load_cli_config()`` bridges ``terminal.*`` into TERMINAL_* env vars for
terminal_tool. It has always carried ``home_mode`` -> TERMINAL_HOME_MODE, but
it exported the raw config value, while the reload bridge in
``hermes_cli.config`` canonicalizes it and skips an unrecognized mode. Both now
route through ``hermes_constants.normalize_terminal_home_mode``, so an alias
means the same thing at startup and after a reload, and neither site can stamp
a mode that ``get_subprocess_home`` would silently read as "auto".
"""

import os

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    hermes_home = tmp_path / "home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    import hermes_cli.config as cfg

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    return hermes_home


def _load_cli_config(home):
    """Call cli.py's standalone loader against *home*.

    cli.py binds ``_hermes_home = get_hermes_home()`` at import time, so
    monkeypatching HERMES_HOME afterwards doesn't move it (see
    test_managed_scope_cli_config.py, which uses the same shim).
    """
    import cli

    cli._hermes_home = home
    return cli.load_cli_config()


def test_cli_bridge_exports_canonical_home_mode(home, monkeypatch):
    (home / "config.yaml").write_text(
        "terminal:\n  backend: local\n  home_mode: real\n", encoding="utf-8"
    )
    monkeypatch.delenv("TERMINAL_HOME_MODE", raising=False)

    _load_cli_config(home)

    assert os.environ["TERMINAL_HOME_MODE"] == "real"


def test_cli_bridge_canonicalizes_an_alias(home, monkeypatch):
    (home / "config.yaml").write_text(
        "terminal:\n  backend: local\n  home_mode: ' Isolated '\n", encoding="utf-8"
    )
    monkeypatch.delenv("TERMINAL_HOME_MODE", raising=False)

    _load_cli_config(home)

    assert os.environ["TERMINAL_HOME_MODE"] == "profile"


def test_cli_bridge_skips_an_unrecognized_home_mode(home, monkeypatch):
    """Skipped, not exported — same rejection the reload bridge applies, so the
    launcher/.env selection survives instead of being replaced by a value that
    reads as "auto"."""
    (home / "config.yaml").write_text(
        "terminal:\n  backend: local\n  home_mode: descriptor\n", encoding="utf-8"
    )
    monkeypatch.setenv("TERMINAL_HOME_MODE", "profile")

    _load_cli_config(home)

    assert os.environ["TERMINAL_HOME_MODE"] == "profile"
