"""Runtime-main config field reads for auxiliary model routing."""
import contextlib
from collections.abc import Callable


def read_main_field(field: str, *, readonly: bool, lower: bool = False) -> str | Callable[[], str]:
    """Runtime override first, then the model block in config.yaml."""
    from agent import auxiliary_client as aux
    override = aux._runtime_main_value(field)
    if field == "api_key" and callable(override):
        return override
    if isinstance(override, str) and override.strip():
        value = override.strip()
        return value.lower() if lower else value
    with contextlib.suppress(Exception):
        from hermes_cli import config as cfg_mod
        cfg = (cfg_mod.load_config_readonly if readonly else cfg_mod.load_config)()
        model_cfg = cfg.get("model", {})
        if field == "model" and isinstance(model_cfg, str) and model_cfg.strip():
            return model_cfg.strip()
        if isinstance(model_cfg, dict):
            value = model_cfg.get("default" if field == "model" else field, "")
            if isinstance(value, str) and value.strip():
                value = value.strip()
                return value.lower() if lower else value
    return ""
