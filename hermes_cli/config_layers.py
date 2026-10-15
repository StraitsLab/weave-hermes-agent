"""Strict and managed policy layers; ordinary interactive reads retain their fallback."""
import copy
from pathlib import Path
from typing import Any

import hermes_yaml as yaml


def read_config_mapping_strict(path: Path) -> dict[str, Any]:
    """Missing/documentless files are empty; null, non-mapping or multiple roots refuse."""
    from ruamel.yaml import YAML
    from ruamel.yaml.nodes import MappingNode
    try:
        with open(path, encoding="utf-8-sig") as stream:
            document = stream.read()
        parser = YAML(typ="safe", pure=True)
        parser.version = (1, 1)
        node = parser.compose(document)
        if node is None:
            return {}
        if not isinstance(node, MappingNode):
            raise TypeError(f"{path}: top-level YAML must be a mapping")
        loaded = yaml.safe_load(document)
        if not isinstance(loaded, dict):
            raise TypeError(f"{path}: top-level YAML must construct a mapping")
        return loaded
    except FileNotFoundError:
        return {}


def load_config_strict() -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    """Read fresh policy layers, failing closed without consulting or seeding caches."""
    from hermes_cli import config as cfg
    from hermes_cli.moa_config import apply_user_moa_presets
    with cfg._CONFIG_LOCK:
        user = read_config_mapping_strict(cfg.get_config_path())
        managed_dir = cfg.managed_scope.get_managed_dir()
        managed = read_config_mapping_strict(managed_dir / "config.yaml") if managed_dir else {}
        config = cfg._deep_merge(copy.deepcopy(cfg.DEFAULT_CONFIG), copy.deepcopy(user))
        apply_user_moa_presets(config, user)
        expanded = cfg._expand_env_vars(cfg._canonicalize_config(config))
        if managed:
            normalized = cfg._normalize_root_model_keys(copy.deepcopy(managed))
            if isinstance(normalized.get("model"), str):
                normalized["model"] = {"default": normalized["model"]}
            expanded = cfg._deep_merge(expanded, cfg._expand_env_vars(normalized))
        return expanded, (user, managed)


def merge_managed_overlay(expanded: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    """Apply the managed-scope overlay; returns ``(merged, managed_config_or_falsy)``.

    Managed wins at the leaf AFTER user expansion, so user ${VAR} cannot shadow
    a managed literal; managed values expand only against the process environment.
    """
    from hermes_cli import config as cfg
    managed_config = cfg.managed_scope.load_managed_config()
    if not managed_config:
        return expanded, managed_config
    # Canonicalize just as the user layer so the result never exposes a nested model dict.
    managed_normalized = cfg._normalize_root_model_keys(managed_config)
    if isinstance(managed_normalized.get("model"), str):
        managed_normalized = dict(managed_normalized)
        managed_normalized["model"] = {"default": managed_normalized["model"]}
    return cfg._deep_merge(expanded, cfg._expand_env_vars(managed_normalized)), managed_config
