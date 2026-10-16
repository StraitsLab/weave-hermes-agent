"""WEV-3464: the model starting a card tool reaches the Harso chat at once, so the apps can draw a placeholder."""
from types import SimpleNamespace

from gateway.native_session_lifecycle import wire_tools


def _agent():
    return SimpleNamespace(tool_start_callback=None, tool_complete_callback=None, tool_gen_callback=None)


def test_a_card_tool_being_written_is_published_and_other_tools_are_not():
    seen = []
    adapter = SimpleNamespace(_native_submit_tool_generating=lambda key, name: seen.append((key, name)))
    agent = _agent()
    wire_tools(agent, adapter, "s1")
    agent.tool_gen_callback("mcp__weave_connectors__show_weather_now")
    agent.tool_gen_callback("web_search")
    assert seen == [("s1", "mcp__weave_connectors__show_weather_now")]


def test_an_existing_generating_callback_still_runs():
    calls = []
    adapter = SimpleNamespace(_native_submit_tool_generating=lambda key, name: calls.append("native"))
    agent = _agent()
    agent.tool_gen_callback = lambda name: calls.append("cli")
    wire_tools(agent, adapter, "s1")
    agent.tool_gen_callback("mcp__weave_connectors__show_output")
    assert calls == ["cli", "native"]
