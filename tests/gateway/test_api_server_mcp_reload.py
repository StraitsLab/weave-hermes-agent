"""Tests for ``POST /v1/mcp/reload`` (WEV-1795 v5).

The endpoint exposes the stock whole-set MCP reload already used by the
``/reload-mcp`` slash command (``GatewayRunner._reload_mcp_toolset``:
shutdown -> discover -> refresh cached agents).  Coverage here:

- auth ordering: unauthenticated -> 401 before any MCP work; named-profile
  mirror (``/p/<profile>/…``) -> 403 before any MCP work; auth runs BEFORE
  the profile gate (bad token on a mirror is 401, not 403).
- classification parity: added/removed/reconnected returned by the endpoint
  equal what the slash-command path reports on the same ``_servers``
  before/after fixture (both paths are called).
- concurrency: a second POST during a slow (blocked stdio shutdown) reload
  gets 409, and ``/health`` keeps answering while the reload runs.
- next-turn adoption: after a reload that changes the tool schema set, a
  cached agent's next turn sees the new schemas (same cached agent object —
  adoption comes from the reload's cached-agent refresh; this is the test
  the ``refresh_agent_mcp_tools``-loop mutant is proved RED against, see
  ``.lane/BUILD-RESULT.md``).

The next-turn test reuses the harness approach from
``.lane-evidence/review-2/run_turn.py``: a capturing ``AIAgent`` subclass and
a bare ``GatewayRunner`` driving the real ``TurnRunner.run_sync`` cache path,
with inference replaced at ``run_conversation`` and the MCP registry seams
stubbed (which also holds ``registry._generation`` static so the cached
agent is REUSED on the next turn and adoption is attributable to the
reload's refresh — a real discovery cycle would also force a cache-rebuild
via the registry-generation cache-bust, i.e. defense in depth not asserted
here).
"""

from __future__ import annotations

import asyncio
import copy
import json
import threading
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter, _api_request_profile
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource

API_KEY = "mcp-reload-test-key-123456"


# ---------------------------------------------------------------------------
# Request / adapter / runner fixtures
# ---------------------------------------------------------------------------

def _make_request(token: str | None = API_KEY, *, method: str = "POST", path: str = "/v1/mcp/reload"):
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return SimpleNamespace(
        headers=headers,
        remote="127.0.0.1",
        transport=None,
        method=method,
        path_qs=path,
    )


def _make_adapter(runner=None) -> APIServerAdapter:
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._api_key = API_KEY
    adapter.gateway_runner = runner
    return adapter


def _make_event() -> MessageEvent:
    source = SessionSource(
        platform=Platform.API_SERVER,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )
    return MessageEvent(text="/reload-mcp", source=source, message_id="m1")


def _make_bare_runner():
    """Bare GatewayRunner with an empty agent cache.

    Same construction as tests/gateway/test_mcp_reload_refreshes_cached_agents.py.
    """
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.API_SERVER: PlatformConfig(enabled=True)}
    )
    runner._agent_cache = OrderedDict()
    runner._agent_cache_lock = threading.Lock()
    return runner


class _McpStub:
    """Deterministic ``_servers`` before -> after transition for one reload."""

    def __init__(self, before: dict, after: dict, discovered: list):
        self.before = dict(before)
        self.after = dict(after)
        self.discovered = list(discovered)
        self.shutdown_calls = 0

    def shutdown(self):
        self.shutdown_calls += 1

    def discover(self):
        import tools.mcp_tool as mcp_tool

        with mcp_tool._lock:
            mcp_tool._servers.clear()
            mcp_tool._servers.update(self.after)
        return list(self.discovered)


def _stub_patches(stub: _McpStub):
    return (
        patch("tools.mcp_tool.shutdown_mcp_servers", side_effect=stub.shutdown),
        patch("tools.mcp_tool.discover_mcp_tools", side_effect=stub.discover),
        patch.dict("tools.mcp_tool._servers", dict(stub.before), clear=True),
    )


async def _call_endpoint(adapter: APIServerAdapter, stub: _McpStub):
    p1, p2, p3 = _stub_patches(stub)
    with p1, p2, p3:
        return await adapter._handle_mcp_reload(_make_request())


async def _call_slash(runner, stub: _McpStub) -> str:
    p1, p2, p3 = _stub_patches(stub)
    with p1, p2, p3:
        return await runner._execute_mcp_reload(_make_event())


# ---------------------------------------------------------------------------
# Auth ordering
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unauthenticated_is_401_before_any_mcp_work():
    runner = MagicMock()
    adapter = _make_adapter(runner=runner)
    with (
        patch("tools.mcp_tool.shutdown_mcp_servers") as shutdown,
        patch("tools.mcp_tool.discover_mcp_tools") as discover,
    ):
        resp = await adapter._handle_mcp_reload(_make_request(token=None))
    assert resp.status == 401
    shutdown.assert_not_called()
    discover.assert_not_called()
    runner._reload_mcp_toolset.assert_not_called()


@pytest.mark.asyncio
async def test_named_profile_mirror_is_403_before_any_mcp_work():
    runner = MagicMock()
    adapter = _make_adapter(runner=runner)
    adapter._expected_api_key = lambda: API_KEY  # auth must PASS here
    token = _api_request_profile.set("worker")
    try:
        with (
            patch("tools.mcp_tool.shutdown_mcp_servers") as shutdown,
            patch("tools.mcp_tool.discover_mcp_tools") as discover,
        ):
            resp = await adapter._handle_mcp_reload(_make_request())
    finally:
        _api_request_profile.reset(token)
    assert resp.status == 403
    shutdown.assert_not_called()
    discover.assert_not_called()
    runner._reload_mcp_toolset.assert_not_called()


@pytest.mark.asyncio
async def test_auth_is_checked_before_the_profile_gate():
    """A bad token on a named-profile mirror must be 401, not 403."""
    runner = MagicMock()
    adapter = _make_adapter(runner=runner)
    adapter._expected_api_key = lambda: API_KEY
    token = _api_request_profile.set("worker")
    try:
        with patch("tools.mcp_tool.shutdown_mcp_servers") as shutdown:
            resp = await adapter._handle_mcp_reload(_make_request(token="wrong-key"))
    finally:
        _api_request_profile.reset(token)
    assert resp.status == 401
    shutdown.assert_not_called()
    runner._reload_mcp_toolset.assert_not_called()


# ---------------------------------------------------------------------------
# Classification parity with the slash-command path
# ---------------------------------------------------------------------------

def _names_reported(text: str, key: str) -> list:
    """Extract the ``{names}`` payload of ``t(key)`` from slash output."""
    from agent.i18n import t

    marker = t(key, names="\x00")
    prefix, _, suffix = marker.partition("\x00")
    for line in text.splitlines():
        if not line.startswith(prefix):
            continue
        if suffix and not line.endswith(suffix):
            continue
        names = line[len(prefix): len(line) - len(suffix) if suffix else None]
        return sorted(n.strip() for n in names.split(",") if n.strip())
    return []


@pytest.mark.asyncio
async def test_classification_parity_with_slash_command_path():
    before = {"alpha": object(), "beta": object()}
    after = {"beta": object(), "gamma": object()}

    runner = _make_bare_runner()
    adapter = _make_adapter(runner=runner)

    endpoint_result = json.loads(
        (await _call_endpoint(adapter, _McpStub(before, after, ["mcp__gamma__x"]))).text
    )
    slash_text = await _call_slash(runner, _McpStub(before, after, ["mcp__gamma__x"]))

    assert sorted(endpoint_result["added"]) == ["gamma"]
    assert sorted(endpoint_result["removed"]) == ["alpha"]
    assert sorted(endpoint_result["reconnected"]) == ["beta"]
    assert endpoint_result["tools"] == 1
    assert endpoint_result["servers"] == 2

    # The slash-command path must report exactly the same classification.
    assert sorted(endpoint_result["added"]) == _names_reported(
        slash_text, "gateway.reload_mcp.added"
    )
    assert sorted(endpoint_result["removed"]) == _names_reported(
        slash_text, "gateway.reload_mcp.removed"
    )
    assert sorted(endpoint_result["reconnected"]) == _names_reported(
        slash_text, "gateway.reload_mcp.reconnected"
    )


# ---------------------------------------------------------------------------
# Concurrency + off-loop behaviour
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_second_reload_conflicts_409_and_health_answers_during_reload():
    adapter = _make_adapter(runner=_make_bare_runner())
    entered = threading.Event()
    release = threading.Event()

    def _slow_shutdown():
        entered.set()
        assert release.wait(20)

    p1 = patch("tools.mcp_tool.shutdown_mcp_servers", side_effect=_slow_shutdown)
    p2 = patch("tools.mcp_tool.discover_mcp_tools", return_value=[])
    p3 = patch.dict("tools.mcp_tool._servers", {}, clear=True)
    with p1, p2, p3:
        first = asyncio.create_task(adapter._handle_mcp_reload(_make_request()))
        for _ in range(500):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        else:  # pragma: no cover - timing failure
            pytest.fail("reload never reached the blocking stdio shutdown")

        second = await asyncio.wait_for(
            adapter._handle_mcp_reload(_make_request()), timeout=5
        )
        assert second.status == 409

        health = await asyncio.wait_for(
            adapter._handle_health(_make_request(method="GET", path="/health")),
            timeout=5,
        )
        assert health.status == 200

        release.set()
        first_resp = await asyncio.wait_for(first, timeout=10)
        assert first_resp.status == 200
        assert json.loads(first_resp.text)["servers"] == 0
    assert adapter._mcp_reload_in_progress is False


# ---------------------------------------------------------------------------
# Next-turn adoption of new schemas by cached agents
# ---------------------------------------------------------------------------

def _fn_def(name: str, description: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _write_config(home) -> None:
    cfg = {
        "model": {
            "default": "synthetic",
            "provider": "custom",
            "base_url": "http://127.0.0.1:1/v1",
        },
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "checkpoints": {"enabled": False},
        "toolsets": [],
        "tools": {"tool_search": False},
        "display": {"tool_progress": "off", "streaming": False},
        "platform_toolsets": {},
        "mcp_servers": {"fixture": {"command": "unused-by-stubbed-discovery"}},
    }
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=True))


def _make_turn_runner():
    """Bare GatewayRunner wired exactly like .lane-evidence/review-2/run_turn.py."""
    from gateway.run import GatewayRunner

    r = GatewayRunner.__new__(GatewayRunner)
    r.config = GatewayConfig()
    r._provider_routing = {}
    r._agent_cache = OrderedDict()
    r._agent_cache_lock = threading.Lock()
    r._running_agents = {}
    r._session_db = None
    r._prefill_messages = None
    r._pending_model_notes = {}
    r._pending_skills_reload_notes = {}
    r.session_store = SimpleNamespace(_entries={})
    r._adapter_for_source = lambda source: None
    r._get_system_prompt_for_channel = lambda *a, **k: None
    runtime = {
        "api_key": "synthetic-no-credential",
        "base_url": "http://127.0.0.1:1/v1",
        "provider": "custom",
        "api_mode": "chat_completions",
    }
    r._resolve_session_agent_runtime = lambda **k: ("synthetic", runtime)
    r._resolve_turn_agent_config = lambda message, model, runtime: {
        "model": model,
        "runtime": runtime,
    }
    r._resolve_session_reasoning_config = lambda **k: None
    r._resolve_session_service_tier = lambda **k: None
    r._refresh_fallback_model = lambda: None
    r._consume_pending_native_image_paths = lambda k: []
    r._consume_pending_turn_sidecar_notes = lambda k: []
    r._consume_pending_relay_turn_id = lambda k: None
    r._sync_session_model_from_agent = lambda *a, **k: None
    return r


def _capturing_agent_class(captures: list):
    from run_agent import AIAgent

    class CapturingAgent(AIAgent):
        def __init__(self, **kwargs):
            kwargs["skip_memory"] = True
            kwargs["skip_context_files"] = True
            kwargs["load_soul_identity"] = False
            super().__init__(**kwargs)

        def run_conversation(self, message, **kwargs):
            # Captures the real built tool schemas at turn time.  The real
            # between-turns MCP refresh lives in the conversation prologue
            # (agent/turn_context.build_turn_context), which this capturing
            # seam replaces — and is deliberately NOT re-applied here, so
            # schema adoption is pinned to the reload's cached-agent refresh
            # (the mutant-proved contract).
            captures.append(
                {"agent_id": id(self), "tools": copy.deepcopy(self.tools)}
            )
            messages = (kwargs.get("conversation_history") or []) + [
                {"role": "user", "content": message},
                {"role": "assistant", "content": "synthetic inference complete"},
            ]
            self._session_messages = copy.deepcopy(messages)
            return {
                "final_response": "synthetic inference complete",
                "messages": messages,
                "completed": True,
                "api_calls": 0,
            }

    return CapturingAgent


def _run_turn(runner, home, route: str, label: str, agent_cls, captures: list, histories: dict) -> dict:
    import gateway.run as gw
    from agent.skill_utils import parse_config_string_list
    from gateway.turn_context import TurnContext

    with gw._profile_runtime_scope(home):
        cfg = gw._load_gateway_config()
        source = SessionSource(
            platform=Platform.API_SERVER, chat_id=route, user_id="synthetic-user"
        )
        enabled = runner._resolve_enabled_toolsets_for_source(
            cfg, source, gw._platform_config_key(source.platform)
        )
        disabled = (
            parse_config_string_list((cfg.get("agent") or {}).get("disabled_toolsets"))
            or None
        )
        ctx = TurnContext(
            source=source,
            message=label,
            history=copy.deepcopy(histories.get(route, [])),
            session_id="mcp-reload-" + route,
            session_key=route,
            user_config=cfg,
            enabled_toolsets=enabled,
            disabled_toolsets=disabled,
            AIAgent=agent_cls,
            resolve_display_setting=lambda *a: False,
            _run_still_current=lambda: True,
            _hooks_ref=SimpleNamespace(loaded_hooks=False),
        )
        before = runner._agent_cache.get(route)
        result = gw.TurnRunner(runner, ctx).run_sync()
        assert not result.get("failed"), result
        histories[route] = result["messages"]
        entry = runner._agent_cache[route]
        return {
            "reused": bool(before and before[0] is entry[0]),
            "agent": entry[0],
            "capture": captures[-1],
        }


def test_next_turn_adopts_new_schemas_for_cached_agents(tmp_path):
    """A reload that changes the tool schema set must reach the next turn.

    The cached agent is REUSED on the next turn (asserted below), so without
    the reload's cached-agent refresh the turn would still see the stale
    schemas — this is the mutation target proved RED in .lane/BUILD-RESULT.md.
    """
    home = tmp_path / "hermes-home"
    _write_config(home)

    captures: list = []
    histories: dict = {}
    agent_cls = _capturing_agent_class(captures)
    runner = _make_turn_runner()
    adapter = _make_adapter(runner=runner)

    v1 = [_fn_def("mcp__fixture__alpha", "fixture A schema v1")]
    v2 = [
        _fn_def("mcp__fixture__alpha", "fixture A schema v2"),
        _fn_def("mcp__fixture__beta", "fixture B schema v2"),
    ]
    current = {"tools": v1}

    def _fake_get_tool_definitions(*args, **kwargs):
        return copy.deepcopy(current["tools"])

    stub = _McpStub(
        before={"fixture": object()},
        after={"fixture": object()},
        discovered=["mcp__fixture__alpha", "mcp__fixture__beta"],
    )

    p0 = patch("run_agent.get_tool_definitions", side_effect=_fake_get_tool_definitions)
    p1 = patch("model_tools.get_tool_definitions", side_effect=_fake_get_tool_definitions)
    p2, p3, p4 = _stub_patches(stub)
    with p0, p1, p2, p3, p4:
        # Turn 1: warm a cached agent carrying schema v1.
        first = _run_turn(runner, home, "route", "warm", agent_cls, captures, histories)
        assert first["reused"] is False
        assert [t["function"]["name"] for t in first["capture"]["tools"]] == [
            "mcp__fixture__alpha"
        ]

        # The schema change arrives; POST /v1/mcp/reload ships it.
        current["tools"] = v2
        resp = asyncio.run(adapter._handle_mcp_reload(_make_request()))
        assert resp.status == 200
        assert json.loads(resp.text)["reconnected"] == ["fixture"]

        # Next turn: the SAME cached agent must expose the new schemas.
        second = _run_turn(runner, home, "route", "next", agent_cls, captures, histories)
        assert second["reused"] is True
        tools_by_name = {t["function"]["name"]: t for t in second["capture"]["tools"]}
        assert sorted(tools_by_name) == ["mcp__fixture__alpha", "mcp__fixture__beta"]
        assert "v2" in tools_by_name["mcp__fixture__alpha"]["function"]["description"]

    # The reload did touch the cached agent's refresh path with a non-empty cache.
    assert stub.shutdown_calls == 1
