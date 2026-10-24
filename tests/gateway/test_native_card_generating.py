"""WEV-3464: the model starting a card tool reaches the Harso chat at once, so the apps can draw a placeholder."""
import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter

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


@pytest.mark.asyncio
@pytest.mark.parametrize("command,preview", [("git status -s", "git"), ("TOKEN=abc ./x", None)])
async def test_tool_start_forwards_arguments_and_emits_only_safe_preview(command, preview, monkeypatch):
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    queue = asyncio.Queue(maxsize=32)
    adapter._native_submit_subscribers["r1"] = (queue, asyncio.get_running_loop())
    adapter._native_submit_active_refs["s1"] = "r1"
    seen = []
    native_start = adapter._native_submit_tool_started

    def start(*args):
        seen.append(args)
        native_start(*args)

    monkeypatch.setattr(adapter, "_native_submit_tool_started", start)
    agent = _agent()
    wire_tools(agent, adapter, "s1")
    args = {"command": command}
    agent.tool_start_callback("c1", "terminal", args)

    assert seen == [("s1", "c1", "terminal", args)]
    event = queue.get_nowait()
    assert event["type"] == "tool.started"
    assert event["tool_call_id"] == "c1"
    assert event["tool_name"] == "terminal"
    if preview is None:
        assert "preview" not in event
    else:
        assert event["preview"] == preview
