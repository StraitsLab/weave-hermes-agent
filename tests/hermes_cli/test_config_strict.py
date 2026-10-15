import os
from unittest.mock import patch

import pytest
from hermes_cli.config import load_config

_NULL_ROOTS = ["null\n", "~\n", "!!null null\n", "---\nnull\n...\n"]
# A mapping NODE that constructs a set: passes a node-kind check, must still refuse.
_SET_ROOTS = ["!!set {}\n", "!<tag:yaml.org,2002:set> {}\n", "%TAG !e! tag:yaml.org,2002:\n--- !e!set {}\n",
              "\ufeff--- !!set\n{}\n...\n", "!!set {a: null}\n", "!!omap []\n", "!!pairs []\n"]


class TestLoadConfigStrict:
    """load_config_strict raises where load_config falls back, and never
    touches the load caches, so plain load_config keeps its fallbacks."""

    def test_raises_where_load_config_serves_last_known_good(self, tmp_path):
        from hermes_cli.config import load_config_strict

        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            cfg = tmp_path / "config.yaml"
            cfg.write_text("cron:\n  allow_scripts: true\n", encoding="utf-8")
            assert load_config()["cron"]["allow_scripts"] is True
            cfg.write_text("cron:\n  allow_scripts: false\nmax_turns: 5\nagent: 1\n", encoding="utf-8")

            with pytest.raises(Exception):
                load_config_strict()
            # Strict mode wrote no cache: load_config still serves the LKG,
            # and caches it under the broken file's signature...
            assert load_config()["cron"]["allow_scripts"] is True
            # ...which strict mode must not serve either.
            with pytest.raises(Exception):
                load_config_strict()

    def test_strict_read_seeds_no_last_known_good(self, tmp_path):
        from hermes_cli.config import load_config_strict

        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            cfg = tmp_path / "config.yaml"
            cfg.write_text("cron:\n  allow_scripts: false\n", encoding="utf-8")
            assert load_config_strict()[0]["cron"]["allow_scripts"] is False
            cfg.write_text("cron: [unclosed\n", encoding="utf-8")
            # Cold plain load: defaults, not a value the strict read left behind.
            assert load_config()["cron"]["allow_scripts"] is True

    @pytest.mark.parametrize("text", [
        "cron: [unclosed\n", "[1, 2]\n", "just a string\n",
        *_NULL_ROOTS, *_SET_ROOTS, "---\n", "a: 1\n---\nb: 2\n",
    ])
    def test_malformed_or_non_mapping_user_config_raises(self, tmp_path, text):
        from hermes_cli.config import load_config_strict

        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
            with pytest.raises(Exception):
                load_config_strict()
            assert isinstance(load_config(), dict)  # plain loader still fails open

    @pytest.mark.parametrize("text", [*_NULL_ROOTS, *_SET_ROOTS, "---\n", "a: 1\n---\nb: 2\n"])
    def test_null_or_multi_document_managed_config_raises(self, tmp_path, text):
        from hermes_cli.config import load_config_strict

        managed = tmp_path / "managed"
        managed.mkdir()
        (managed / "config.yaml").write_text(text, encoding="utf-8")
        home = tmp_path / "home"
        home.mkdir()
        with patch.dict(os.environ, {"HERMES_HOME": str(home), "HERMES_MANAGED_DIR": str(managed)}):
            with pytest.raises(Exception):
                load_config_strict()
            assert isinstance(load_config(), dict)

    @pytest.mark.parametrize("text", ["", "  \n\n", "# comment only\n"], ids=["empty", "blank", "comment"])
    def test_documentless_config_is_an_empty_layer(self, tmp_path, text):
        from hermes_cli.config import load_config_strict

        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
            effective, layers = load_config_strict()
        assert layers == ({}, {})
        assert effective["cron"]["allow_scripts"] is True

    def test_strict_mapping_keeps_anchors_and_aliases(self, tmp_path):
        from hermes_cli.config import load_config_strict

        text = "base: &b {allow_scripts: false}\ncron: *b\n"
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
            effective, (user, _) = load_config_strict()
        assert user["cron"] == {"allow_scripts": False}
        assert effective["cron"]["allow_scripts"] is False

    def test_malformed_managed_config_raises_but_load_config_fails_open(self, tmp_path):
        from hermes_cli.config import load_config_strict

        managed = tmp_path / "managed"
        managed.mkdir()
        (managed / "config.yaml").write_text("cron: {allow_scripts: [\n", encoding="utf-8")
        home = tmp_path / "home"
        home.mkdir()
        with patch.dict(os.environ, {"HERMES_HOME": str(home), "HERMES_MANAGED_DIR": str(managed)}):
            with pytest.raises(Exception):
                load_config_strict()
            assert isinstance(load_config(), dict)

    def test_returns_effective_and_raw_layers(self, tmp_path):
        from hermes_cli.config import load_config_strict

        managed = tmp_path / "managed"
        managed.mkdir()
        (managed / "config.yaml").write_text("cron:\n  allow_scripts: false\n", encoding="utf-8")
        home = tmp_path / "home"
        home.mkdir()
        (home / "config.yaml").write_text("cron:\n  allow_scripts: true\n  platform_script_digests: null\n", encoding="utf-8")
        with patch.dict(os.environ, {"HERMES_HOME": str(home), "HERMES_MANAGED_DIR": str(managed)}):
            effective, (user, managed_layer) = load_config_strict()
        assert effective["cron"]["allow_scripts"] is False
        assert user == {"cron": {"allow_scripts": True, "platform_script_digests": None}}
        assert managed_layer == {"cron": {"allow_scripts": False}}

    def test_missing_files_are_empty_layers(self, tmp_path):
        from hermes_cli.config import load_config_strict

        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            effective, layers = load_config_strict()
        assert layers == ({}, {})
        assert effective["cron"]["allow_scripts"] is True
