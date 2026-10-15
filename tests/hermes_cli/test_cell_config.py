"""Documented cell overrides must be native config and reach their consumers."""
from pathlib import Path
import re

import hermes_yaml as yaml
from hermes_cli.config_defaults import DEFAULT_CONFIG


def test_cell_overrides_are_native_and_disable_curator(tmp_path, monkeypatch):
    from hermes_cli.config import load_config
    from agent import curator
    text = (Path(__file__).resolve().parents[2] / "docs/harso-cell-config.md").read_text()
    source = re.search(r"```yaml\n(.*?)```", text, re.DOTALL).group(1)
    overrides = yaml.safe_load(source)

    def native(keys, defaults):
        for key, value in keys.items():
            assert key in defaults
            if isinstance(value, dict):
                native(value, defaults[key])
    native(overrides, DEFAULT_CONFIG)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(source)
    cfg = load_config()
    for section, key in [("sessions", "auto_prune"), ("curator", "enabled"), ("nous", "guest")]:
        assert cfg[section][key] is False
    assert curator.is_enabled() is False
    assert curator.should_run_now() is False
