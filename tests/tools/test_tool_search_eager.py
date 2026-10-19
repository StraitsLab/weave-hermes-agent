"""Pinned browser/computer schemas remain discoverable without executing tools."""
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def eager_surface(monkeypatch):
    importlib.import_module("model_tools")  # Bind global consumers before isolating the registry.
    from tools import tool_search as ts
    from tools.registry import ToolRegistry

    registry = ToolRegistry()
    monkeypatch.setattr("tools.registry.registry", registry)
    spec = importlib.util.spec_from_file_location(
        "eager_browse_plugin", Path(__file__).resolve().parents[2] / "plugins/harso-jev/__init__.py")
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    schemas = [plugin.SCHEMA, {
        "name": "mcp__weave_connectors__terminal",
        "description": "Run one command on the user's computer (argv, no shell).",
        "parameters": {"type": "object", "properties": {
            "argv": {"type": "array", "items": {"type": "string"}}}, "required": ["argv"]},
    }]
    defs = [{"type": "function", "function": schema} for schema in schemas]

    def never_execute(*args, **kwargs):
        pytest.fail("Metadata reads must not execute browser/computer tools")

    for schema, toolset in zip(schemas, ["harso-jev", "mcp-weave-connectors"]):
        registry.register(name=schema["name"], toolset=toolset, schema=schema, handler=never_execute)
    config = ts.ToolSearchConfig.from_raw({"always_eager": [schema["name"] for schema in schemas]})
    monkeypatch.setattr(ts, "load_config", lambda: config)
    monkeypatch.setattr(ts, "load_config_readonly", lambda: config)
    return ts, config, defs


def test_always_eager_browser_computer_names_are_searchable(eager_surface):
    ts, config, defs = eager_surface
    # Pinning changes assembly, not whether an available capability exists.
    assembled = ts.assemble_tool_defs(defs, config=config)
    assert assembled.tool_defs == defs
    result = json.loads(ts.dispatch_tool_search(
        {"queries": ["browse", "terminal"], "limit": 2}, current_tool_defs=defs))
    assert [row["matches"] for row in result["results"]] == [
        ["browse"], ["mcp__weave_connectors__terminal"]]
    assert result["total_available"] == 2


def test_always_eager_schemas_can_be_described(eager_surface):
    ts, _, defs = eager_surface
    names = [td["function"]["name"] for td in defs]
    result = json.loads(ts.dispatch_tool_describe({"names": names}, current_tool_defs=defs))
    assert result == {"tools": {td["function"]["name"]: {
        "description": td["function"]["description"],
        "parameters": td["function"]["parameters"]} for td in defs}}


def test_metadata_never_exposes_eager_tools_outside_session_scope(eager_surface):
    ts, _, defs = eager_surface
    names = [td["function"]["name"] for td in defs]
    described = json.loads(ts.dispatch_tool_describe({"names": names}, current_tool_defs=[]))
    assert described["tools"] == {} and described["not_found"] == names
    searched = json.loads(ts.dispatch_tool_search({"queries": ["browse", "terminal"]}, current_tool_defs=[]))
    assert searched["tools"] == {} and searched["total_available"] == 0


def test_real_metadata_bridge_retains_session_selection(eager_surface, monkeypatch):
    import model_tools
    _, _, defs = eager_surface
    enabled, disabled = ["harso-jev", "mcp-weave-connectors"], ["terminal"]

    def selected(**kwargs):
        assert kwargs == {"enabled_toolsets": enabled, "disabled_toolsets": disabled,
                          "quiet_mode": True, "skip_tool_search_assembly": True}
        return defs

    monkeypatch.setattr(model_tools, "get_tool_definitions", selected)
    result, dispatch = model_tools._dispatch_bridge_tool(
        "tool_describe", {"names": ["browse", "mcp__weave_connectors__terminal"]}, enabled, disabled)
    assert set(json.loads(result)["tools"]) == {"browse", "mcp__weave_connectors__terminal"}
    assert dispatch is None
