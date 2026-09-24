"""Fork contract for the ported vault core (upstream @49b4286a22): it lands INERT.

Two gates, both required for a session to see a vault tool:

1. Membership: the vault tools join the ``browser`` toolset through their registry registration only; no
   static toolset in ``toolsets.py`` names them.
2. Opt-in (fork debt): their ``check_fn`` is False unless the profile's config.yaml sets
   ``vault.enabled: true``. Upstream has no such gate, so a default CLI/API/messaging session that resolves
   ``browser`` with a browser available would otherwise expose all five tools.

A Harso cell's materialized selection (``platform_toolsets.{cli,api_server}`` = file/skills/terminal/web)
fails gate 1 as well, so no cell sees one until the materializer selects ``browser`` AND sets the opt-in (V5).
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import pytest
import yaml

VAULT_TOOLS = {"browser_vault_list", "browser_vault_unlock", "browser_vault_fill",
               "browser_vault_save_login", "browser_vault_enter_code"}

# Shape of the cell seed written by weave-cloud profile-authority materializer._native_toolset_config.
_CELL_TOOLSETS = ["file", "skills", "terminal", "web"]
_CELL_CONFIG = {"platform_toolsets": {"cli": list(_CELL_TOOLSETS), "api_server": list(_CELL_TOOLSETS)}}
_DEFAULT_PLATFORMS = ("cli", "api_server", "telegram")


@pytest.fixture(autouse=True)
def _registered():
    import model_tools  # noqa: F401 — triggers tool discovery
    from tools import browser_vault_tool  # noqa: F401
    from tools.registry import registry

    assert VAULT_TOOLS <= {e.name for e in registry.get_all_entries()}


def _names(defs):
    return {d["function"]["name"] for d in defs}


@contextmanager
def _browser_available(browser_use: bool):
    """Both browser stacks: built-in (agent-browser) and Browser Use mode (browser_exec)."""
    with patch("tools.browser_tool.check_browser_requirements", return_value=not browser_use), \
         patch("tools.browser_use_cli.is_browser_use_cli_mode", return_value=browser_use):
        yield


def _write_config(cfg: dict) -> None:
    """Write the profile's real config.yaml (HERMES_HOME is sandboxed per test by tests/conftest.py)."""
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _session_tools(config: dict, platform: str) -> tuple[list, set]:
    from hermes_cli.tools_config import _get_platform_tools
    from model_tools import get_tool_definitions

    enabled = sorted(_get_platform_tools(config, platform, include_default_mcp_servers=False))
    defs = get_tool_definitions(enabled_toolsets=enabled, quiet_mode=True, skip_tool_search_assembly=True)
    return enabled, _names(defs)


def test_no_static_toolset_names_a_vault_tool():
    import toolsets

    for name in toolsets.TOOLSETS:
        static = set(toolsets.resolve_toolset(name, include_registry=False))
        assert not (static & VAULT_TOOLS), (name, static & VAULT_TOOLS)


def test_only_the_browser_toolset_resolves_to_vault_tools():
    import toolsets

    exposing = {name for name in toolsets.TOOLSETS if set(toolsets.resolve_toolset(name)) & VAULT_TOOLS}
    assert exposing == {"browser"}
    assert VAULT_TOOLS <= set(toolsets.resolve_toolset("browser"))


def test_opt_in_defaults_off():
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    from tools.browser_vault_tool import _vault_opted_in

    assert DEFAULT_CONFIG["vault"]["enabled"] is False
    assert _vault_opted_in() is False  # no config.yaml at all


@pytest.mark.parametrize("browser_use", [False, True], ids=["builtin-browser", "browser-use"])
@pytest.mark.parametrize("platform", _DEFAULT_PLATFORMS)
def test_cold_default_session_with_a_browser_exposes_no_vault_tool(platform, browser_use):
    """Reviewer finding 2: no config at all (cold platform inference) resolves `browser` on these platforms,
    and a browser is available, yet no vault tool is offered until the profile opts in."""
    with _browser_available(browser_use):
        enabled, names = _session_tools({}, platform)
    assert "browser" in enabled, (platform, enabled)  # premise: the default composite really selects browser
    assert not (names & VAULT_TOOLS), (platform, names & VAULT_TOOLS)


@pytest.mark.parametrize("platform", _DEFAULT_PLATFORMS)
def test_opting_in_is_what_turns_the_vault_on_for_a_default_session(platform):
    """Positive control on the same path, through the real config.yaml read: the inert result above is the
    gate's doing, not a resolver that can never surface the tools."""
    _write_config({"vault": {"enabled": True}})
    with _browser_available(False):
        _, names = _session_tools({}, platform)
    assert VAULT_TOOLS <= names, (platform, VAULT_TOOLS - names)


@pytest.mark.parametrize("value", [False, "true", 1, None])
def test_only_a_literal_true_opts_in(value):
    from tools.browser_vault_tool import _check_vault_available

    _write_config({"vault": {"enabled": value}})
    with _browser_available(False):
        assert _check_vault_available() is False


def test_cell_selection_exposes_no_vault_tool_even_when_opted_in_with_a_browser():
    """Membership gate alone: a cell selection without `browser` stays inert even if the opt-in is set."""
    _write_config({"vault": {"enabled": True}})
    for platform in ("cli", "api_server"):
        with _browser_available(False):
            enabled, names = _session_tools(_CELL_CONFIG, platform)
        assert "browser" not in enabled, (platform, enabled)
        assert not (names & VAULT_TOOLS), (platform, names & VAULT_TOOLS)


def test_input_tool_schema_untouched_when_the_vault_is_absent():
    """The vault rewriters must not change any schema byte in a session without the vault tools."""
    import model_tools

    browser_type = model_tools._fn_def({"name": "browser_type", "description": "Type text."})
    out = model_tools._apply_dynamic_schemas([browser_type, model_tools._fn_def({"name": "terminal", "description": "x"})])
    assert out[0] is browser_type
    with_vault = model_tools._apply_dynamic_schemas([browser_type, model_tools._fn_def({"name": "browser_vault_fill",
                                                                                        "description": "x"})])
    assert "Vault note" in with_vault[0]["function"]["description"]
