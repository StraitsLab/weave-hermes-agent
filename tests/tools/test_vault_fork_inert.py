"""Fork contract for the ported vault core (upstream @49b4286a22): it lands INERT.

The vault tools join the ``browser`` toolset through their registry registration only; no static
toolset in ``toolsets.py`` names them. So a session sees a vault tool iff its resolved toolset
selection includes ``browser`` (and a browser is available). A Harso cell's materialized selection
(``platform_toolsets.{cli,api_server}`` = file/skills/terminal/web) does not, so no cell sees one
until the materializer selects ``browser`` (V5).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

VAULT_TOOLS = {"browser_vault_list", "browser_vault_unlock", "browser_vault_fill",
               "browser_vault_save_login", "browser_vault_enter_code"}

# Shape of the cell seed written by weave-cloud profile-authority materializer._native_toolset_config.
_CELL_TOOLSETS = ["file", "skills", "terminal", "web"]
_CELL_CONFIG = {"platform_toolsets": {"cli": list(_CELL_TOOLSETS), "api_server": list(_CELL_TOOLSETS)}}


@pytest.fixture(autouse=True)
def _registered():
    import model_tools  # noqa: F401 — triggers tool discovery
    from tools import browser_vault_tool  # noqa: F401
    from tools.registry import registry

    assert VAULT_TOOLS <= {e.name for e in registry.get_all_entries()}


def _names(defs):
    return {d["function"]["name"] for d in defs}


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


def test_cell_selection_exposes_no_vault_tool_even_with_a_browser_available():
    from hermes_cli.tools_config import _get_platform_tools
    from model_tools import get_tool_definitions

    for platform in ("cli", "api_server"):
        enabled = sorted(_get_platform_tools(_CELL_CONFIG, platform))
        assert "browser" not in enabled, (platform, enabled)
        with patch("tools.browser_tool.check_browser_requirements", return_value=True):
            defs = get_tool_definitions(enabled_toolsets=enabled, quiet_mode=True, skip_tool_search_assembly=True)
        assert not (_names(defs) & VAULT_TOOLS), (platform, _names(defs) & VAULT_TOOLS)


def test_selecting_browser_is_what_turns_the_vault_on():
    """Positive control: the same resolution path DOES surface the vault once `browser` is selected, so the
    inert assertion above is not vacuous. Pre-assembly catalog: the vault tools are not in
    _HERMES_CORE_TOOLS in this fork, so an active tool_search bridge defers them (reachable via tool_call);
    a face that enables `browser` pins them with tool_search.always_eager (V5)."""
    from model_tools import get_tool_definitions

    with patch("tools.browser_tool.check_browser_requirements", return_value=True), \
         patch("tools.browser_use_cli.is_browser_use_cli_mode", return_value=False):
        defs = get_tool_definitions(enabled_toolsets=[*_CELL_TOOLSETS, "browser"], quiet_mode=True,
                                    skip_tool_search_assembly=True)
    assert VAULT_TOOLS <= _names(defs), VAULT_TOOLS - _names(defs)


def test_input_tool_schema_untouched_when_the_vault_is_absent():
    """The vault rewriters must not change any schema byte in a session without the vault tools."""
    import model_tools

    browser_type = model_tools._fn_def({"name": "browser_type", "description": "Type text."})
    out = model_tools._apply_dynamic_schemas([browser_type, model_tools._fn_def({"name": "terminal", "description": "x"})])
    assert out[0] is browser_type
    with_vault = model_tools._apply_dynamic_schemas([browser_type, model_tools._fn_def({"name": "browser_vault_fill",
                                                                                        "description": "x"})])
    assert "Vault note" in with_vault[0]["function"]["description"]
