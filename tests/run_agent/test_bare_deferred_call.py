"""A deferred tool called by its bare name runs through the tool_call bridge in the same step (WEV-3395).

With tool search on, a model that has just tool_describe'd a deferred tool often calls it by its own
name. Without this, the fuzzy name repair maps the long shared MCP prefix onto an eager sibling
(show_flight_status -> browse) and the card data runs in the wrong tool. Upstream: NousResearch#130153.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent

P = "mcp__weave_connectors__"
CARD = P + "show_flight_status"


def _defs(*names):
    return [{"type": "function", "function": {"name": n, "description": n, "parameters": {"type": "object"}}}
            for n in names]


@pytest.fixture()
def agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=_defs(P + "browse", "tool_search", "tool_describe", "tool_call")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1",
                    quiet_mode=True, skip_context_files=True, skip_memory=True)
    a.client = MagicMock()
    a._cached_system_prompt = "You are helpful."
    a._use_prompt_caching = False
    a.compression_enabled = False
    a.save_trajectories = False
    return a


def _response(content, tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="tool_calls" if tool_calls else "stop")],
                           model="test/model", usage=None)


def _call(name, arguments):
    return SimpleNamespace(id="c1", type="function", function=SimpleNamespace(name=name, arguments=arguments))


def _run(agent, name, scoped):
    data = '{"number": "SQ 638", "status": "on_time"}'
    agent.client.chat.completions.create.side_effect = [_response("", [_call(name, data)]), _response("Done")]
    with (
        patch("agent.tool_executor._tool_search_scoped_names", return_value=frozenset(scoped)),
        patch("tools.tool_search.is_deferrable_tool_name", side_effect=lambda n: n == CARD),
        patch("run_agent.handle_function_call", return_value='{"shown": true}') as dispatch,
        patch.object(agent, "_persist_session"), patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("Is SQ 638 on time? Show it as a card.")
    return result, dispatch, data


def test_bare_deferred_name_runs_through_tool_call_in_the_same_step(agent):
    result, dispatch, data = _run(agent, CARD, {CARD})
    assert result["api_calls"] == 2  # the card call, then the answer: no retry step
    name, args = dispatch.call_args.args[:2]
    assert name == CARD and args == json.loads(data)  # the bridge unwrap ran the card tool itself
    calls = [tc for m in result["messages"] if m.get("role") == "assistant" for tc in m.get("tool_calls") or []]
    assert calls[0]["function"]["name"] == "tool_call"  # history shows the bridge form the model should use
    assert json.loads(calls[0]["function"]["arguments"]) == {"name": CARD, "arguments": json.loads(data)}


def test_name_outside_the_session_scope_is_never_bridged(agent):
    result, dispatch, _ = _run(agent, CARD, set())
    assert CARD not in [c.args[0] for c in dispatch.call_args_list]  # the bridge keeps its scope gate
    calls = [tc for m in result["messages"] if m.get("role") == "assistant" for tc in m.get("tool_calls") or []]
    assert "tool_call" not in [c["function"]["name"] for c in calls]
