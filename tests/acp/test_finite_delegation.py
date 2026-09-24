"""Finite ACP turns use native model dispatch, child execution and batch join.

Only provider I/O and child construction are substituted. No delegation,
aggregation, capability, prompt-loop or cancellation implementation is mocked.
"""
import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp.schema import TextContentBlock

from acp_adapter.server import HermesACPAgent
from acp_adapter.session import SessionManager
from gateway.session_context import async_delivery_supported
import tools.delegate_tool as dt


def response(content=None, tool_calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=content, tool_calls=tool_calls),
            finish_reason="tool_calls" if tool_calls else "stop",
        )], model="test/model", usage=None,
    )


def real_agent(depth=0):
    from run_agent import AIAgent

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-not-a-credential", model="test/model",
            base_url="https://example.invalid/v1", max_iterations=5,
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            platform="subagent" if depth else "acp",
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "Complete the task using the tool results."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._fallback_chain = []
    agent._delegate_depth = depth
    agent._delegate_role = "leaf"
    agent._persist_disabled = True
    agent._session_db = None
    agent._session_json_enabled = False
    agent._use_streaming = False
    return agent


async def wait_event(event):
    assert await asyncio.to_thread(event.wait, 10), "worker did not reach gate"


@pytest.mark.asyncio
@pytest.mark.parametrize("count,outcome", [
    (1, "success"), (2, "success"), (2, "failure"), (2, "cancel"),
])
async def test_prompt_joins_model_dispatched_children(monkeypatch, tmp_path, count, outcome):
    parent = real_agent()
    parent.valid_tool_names = {"delegate_task"}
    children = [real_agent(1) for _ in range(count)]
    started = [threading.Event() for _ in children]
    release = [threading.Event() for _ in children]
    finished = [threading.Event() for _ in children]
    capabilities = []
    observed = []

    def build_child(**kwargs):
        child = children[kwargs["task_index"]]
        with parent._active_children_lock:
            parent._active_children.append(child)
        return child

    monkeypatch.setattr(dt, "_build_child_agent", build_child)
    monkeypatch.setattr(dt, "_load_config", lambda: {"max_iterations": 5})
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: {
        "model": "test/model", "provider": None, "base_url": None,
        "api_key": None, "api_mode": None, "command": None, "args": None,
    })
    for i, child in enumerate(children):
        def child_model(i=i, **kwargs):
            capabilities.append(async_delivery_supported())
            started[i].set()
            assert release[i].wait(10), "test did not release child"
            finished[i].set()
            if outcome == "failure" and i == 1:
                import httpx
                from openai import BadRequestError
                raise BadRequestError(
                    "deterministic child rejection",
                    response=httpx.Response(400, request=httpx.Request(
                        "POST", "https://example.invalid/v1")), body=None,
                )
            return response(f"verified child result {i}")
        child.client.chat.completions.create.side_effect = child_model

    args = {"background": True}
    if count == 1:
        args["goal"] = "Inspect the native cancellation boundary and report evidence"
    else:
        args["tasks"] = [
            {"goal": f"Inspect native boundary number {i} and report evidence"}
            for i in range(count)
        ]
    call = SimpleNamespace(
        id="delegate-call", type="function",
        function=SimpleNamespace(name="delegate_task", arguments=json.dumps(args)),
    )

    def parent_model(**kwargs):
        tools = [m for m in kwargs["messages"] if m.get("role") == "tool"]
        if not tools:
            return response(tool_calls=[call])
        result = json.loads(tools[-1]["content"])
        observed.append(result)
        return response("Synthesis: " + "; ".join(
            r.get("summary") or r.get("error") or r["status"]
            for r in result.get("results", [])
        ))

    parent.client.chat.completions.create.side_effect = parent_model
    manager = SessionManager(agent_factory=lambda: parent)
    server = HermesACPAgent(session_manager=manager)
    server._conn = MagicMock(session_update=AsyncMock())
    state = manager.create_session(cwd=str(tmp_path))
    task = asyncio.create_task(server.prompt(
        prompt=[TextContentBlock(type="text", text="Delegate and synthesize")],
        session_id=state.session_id,
    ))
    try:
        for event in started:
            await wait_event(event)
        # Both children reached their gates: native batch execution is parallel.
        done, _ = await asyncio.wait({task}, timeout=0.1)
        assert not done, "ACP ended before gated delegated results were joined"
        if outcome == "cancel":
            await server.cancel(state.session_id)
            assert parent._interrupt_requested
            assert all(c._interrupt_requested for c in children)
            for gate in release:
                gate.set()
            result = await asyncio.wait_for(asyncio.shield(task), 15)
            assert result.stop_reason == "cancelled"
            assert not observed, "cancelled parent must not synthesize success"
            assert not state.is_running
            tool_results = [json.loads(m["content"]) for m in state.history
                            if m.get("role") == "tool" and m.get("name") == "delegate_task"]
            assert tool_results
            assert all(r["status"] == "interrupted"
                       for r in tool_results[-1]["results"])
            return
        if count == 2:
            release[1].set()
            await wait_event(finished[1])
            done, _ = await asyncio.wait({task}, timeout=0.1)
            assert not done, "ACP ended after only the faster child"
        release[0].set()
        result = await asyncio.wait_for(asyncio.shield(task), 15)
        assert result.stop_reason == "end_turn"
        if outcome == "failure":
            assert observed[0]["results"][0]["status"] == "completed"
            failed = observed[0]["results"][1]
            assert failed["status"] == "failed"
            assert failed["exit_reason"] == "error"
            assert "deterministic child rejection" in failed["error"]
        else:
            assert [r["summary"] for r in observed[0]["results"]] == [
                f"verified child result {i}" for i in range(count)
            ]
            assert [r["status"] for r in observed[0]["results"]] == ["completed"] * count
        assert "SYNCHRONOUSLY" in observed[0]["note"]
        assert capabilities == [False] * count
        messages = [c.args[1] for c in server._conn.session_update.call_args_list
                    if len(c.args) > 1]
        assert any(getattr(getattr(m, "content", None), "text", "").startswith(
            "Synthesis: verified child result 0") for m in messages)
        # Executor context binding must not turn the caller/interactive lane finite.
        assert async_delivery_supported() is True
    finally:
        for gate in release:
            gate.set()
        await asyncio.wait_for(asyncio.shield(task), 15)
        for event in finished:
            await wait_event(event)
        # A regression/mutation takes the detached path. Drain its native
        # workers before fixture teardown so they cannot touch the next home.
        from tools.async_delegation import active_count
        async def drain_mutant_workers():
            while active_count():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(drain_mutant_workers(), 15)
