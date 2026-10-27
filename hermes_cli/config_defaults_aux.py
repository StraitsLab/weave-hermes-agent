"""Auxiliary-task default block shared by the configuration data."""


def auxiliary_defaults(timeout, *, reasoning_effort=True, **extra):
    """reasoning_effort=False omits that key (MoA blocks configure depth per slot)."""
    defaults = {"provider": "auto", "model": "", "base_url": "", "api_key": "", "timeout": timeout, "extra_body": {}}
    if reasoning_effort:
        defaults["reasoning_effort"] = ""
    defaults.update(extra)
    return defaults


def camofox_defaults():
    return {
        # Stable profile-scoped userId; false gives an ephemeral userId per session.
        "managed_persistence": False,
        # Externally managed identity for an app-owned browser.
        "user_id": "",
        "session_key": "",
        "adopt_existing_tab": False,
        # Rewrite loopback page URLs, not CAMOFOX_URL, for Docker Camofox.
        "rewrite_loopback_urls": False,
        "loopback_host_alias": "host.docker.internal",
    }
