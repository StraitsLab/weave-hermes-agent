"""Auxiliary-task default block shared by the configuration data."""


def auxiliary_defaults(timeout, *, reasoning_effort=True, **extra):
    """reasoning_effort=False omits that key (MoA blocks configure depth per slot)."""
    defaults = {"provider": "auto", "model": "", "base_url": "", "api_key": "", "timeout": timeout, "extra_body": {}}
    if reasoning_effort:
        defaults["reasoning_effort"] = ""
    defaults.update(extra)
    return defaults
