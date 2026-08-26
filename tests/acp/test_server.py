"""Tests for acp_adapter.server — HermesACPAgent ACP server."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

import acp
from acp.agent.router import build_agent_router
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    AgentPlanUpdate,
    AgentThoughtChunk,
    AuthenticateResponse,
    AvailableCommandsUpdate,
    Implementation,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptResponse,
    ResumeSessionResponse,
    SessionModelState,
    SessionModeState,
    SetSessionConfigOptionResponse,
    SetSessionModelResponse,
    SetSessionModeResponse,
    SessionInfo,
    SessionInfoUpdate,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    UsageUpdate,
    UserMessageChunk,
)
from acp_adapter.auth import TERMINAL_SETUP_AUTH_METHOD_ID
from acp_adapter.server import (
    ACP_MAX_MODELS_PER_PROVIDER,
    HermesACPAgent,
    HERMES_VERSION,
)
from acp_adapter.session import SessionManager
from hermes_state import SessionDB


@pytest.fixture()
def mock_manager():
    """SessionManager with a mock agent factory."""
    return SessionManager(agent_factory=lambda: MagicMock(name="MockAIAgent"))


@pytest.fixture()
def agent(mock_manager):
    """HermesACPAgent backed by a mock session manager."""
    return HermesACPAgent(session_manager=mock_manager)


@pytest.mark.asyncio
async def test_new_session_exposes_edit_approvals_as_modes_not_config_options(agent):
    resp = await agent.new_session(cwd="/tmp")

    assert resp.config_options is None
    assert isinstance(resp.modes, SessionModeState)
    assert resp.modes.current_mode_id == "default"
    assert [(mode.id, mode.name) for mode in resp.modes.available_modes] == [
        ("default", "Default"),
        ("accept_edits", "Accept Edits"),
        ("dont_ask", "Don't Ask"),
    ]


@pytest.mark.asyncio
async def test_set_config_option_persists_edit_approval_policy_without_advertising_config(agent):
    resp = await agent.new_session(cwd="/tmp")
    update = await agent.set_config_option(
        "edit_approval_policy",
        resp.session_id,
        "workspace_session",
    )
    state = agent.session_manager.get_session(resp.session_id)

    assert isinstance(update, SetSessionConfigOptionResponse)
    assert update.config_options == []
    assert getattr(state, "mode", None) == "accept_edits"


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------


class TestInitialize:
    @pytest.mark.asyncio
    async def test_initialize_returns_correct_protocol_version(self, agent):
        resp = await agent.initialize(protocol_version=1)
        assert isinstance(resp, InitializeResponse)
        assert resp.protocol_version == acp.PROTOCOL_VERSION




    @pytest.mark.asyncio
    async def test_initialize_advertises_provider_and_terminal_auth_methods(self, agent, monkeypatch):
        monkeypatch.setattr("acp_adapter.auth.detect_provider", lambda: "openrouter")
        monkeypatch.setattr("acp_adapter.server.detect_provider", lambda: "openrouter")

        resp = await agent.initialize(protocol_version=1)
        payloads = [method.model_dump(by_alias=True, exclude_none=True) for method in resp.auth_methods]

        assert payloads[0]["id"] == "openrouter"
        assert payloads[0]["name"] == "openrouter runtime credentials"
        terminal = next(payload for payload in payloads if payload["id"] == TERMINAL_SETUP_AUTH_METHOD_ID)
        assert terminal["type"] == "terminal"
        assert terminal["args"] == ["--setup"]



# ---------------------------------------------------------------------------
# authenticate
# ---------------------------------------------------------------------------


class TestAuthenticate:
    @pytest.mark.asyncio
    async def test_authenticate_with_matching_method_id(self, agent, monkeypatch):
        monkeypatch.setattr(
            "acp_adapter.server.detect_provider",
            lambda: "openrouter",
        )
        resp = await agent.authenticate(method_id="openrouter")
        assert isinstance(resp, AuthenticateResponse)

    @pytest.mark.asyncio
    async def test_authenticate_is_case_insensitive(self, agent, monkeypatch):
        monkeypatch.setattr(
            "acp_adapter.server.detect_provider",
            lambda: "openrouter",
        )
        resp = await agent.authenticate(method_id="OpenRouter")
        assert isinstance(resp, AuthenticateResponse)

    @pytest.mark.asyncio
    async def test_authenticate_rejects_mismatched_method_id(self, agent, monkeypatch):
        monkeypatch.setattr(
            "acp_adapter.server.detect_provider",
            lambda: "openrouter",
        )
        resp = await agent.authenticate(method_id="totally-invalid-method")
        assert resp is None

    @pytest.mark.asyncio
    async def test_authenticate_without_provider(self, agent, monkeypatch):
        monkeypatch.setattr(
            "acp_adapter.server.detect_provider",
            lambda: None,
        )
        resp = await agent.authenticate(method_id="openrouter")
        assert resp is None

    @pytest.mark.asyncio
    async def test_authenticate_accepts_terminal_setup_after_provider_configured(self, agent, monkeypatch):
        monkeypatch.setattr(
            "acp_adapter.server.detect_provider",
            lambda: "openrouter",
        )
        resp = await agent.authenticate(method_id=TERMINAL_SETUP_AUTH_METHOD_ID)
        assert isinstance(resp, AuthenticateResponse)



# ---------------------------------------------------------------------------
# new_session / cancel / load / resume
# ---------------------------------------------------------------------------


class TestSessionOps:

    @pytest.mark.asyncio
    async def test_new_session_returns_authenticated_cross_provider_model_state(self):
        manager = SessionManager(
            agent_factory=lambda: SimpleNamespace(
                model="gpt-5.4",
                provider="openai-codex",
                base_url="https://api.openai.com/v1",
            )
        )
        acp_agent = HermesACPAgent(session_manager=manager)
        picker_context = MagicMock()
        picker_context.with_overrides.return_value = picker_context
        payload = {
            "providers": [
                {
                    "slug": "anthropic",
                    "name": "Anthropic",
                    "models": ["claude-sonnet-4-6", "claude-sonnet-4-6"],
                },
                {
                    "slug": "openai-codex",
                    "name": "OpenAI Codex",
                    "models": [
                        {"id": "gpt-5.4"},
                        "gpt-5.4-mini",
                    ],
                },
            ],
        }

        with (
            patch("hermes_cli.inventory.load_picker_context", return_value=picker_context),
            patch("hermes_cli.inventory.build_models_payload", return_value=payload) as build_payload,
        ):
            resp = await acp_agent.new_session(cwd="/tmp")

        assert isinstance(resp.models, SessionModelState)
        assert resp.models.current_model_id == "openai-codex:gpt-5.4"
        assert [model.model_id for model in resp.models.available_models] == [
            "anthropic:claude-sonnet-4-6",
            "openai-codex:gpt-5.4",
            "openai-codex:gpt-5.4-mini",
        ]
        assert [model.name for model in resp.models.available_models] == [
            "Anthropic · claude-sonnet-4-6",
            "OpenAI Codex · gpt-5.4",
            "OpenAI Codex · gpt-5.4-mini",
        ]
        assert resp.models.available_models[1].description is not None
        assert "current" in resp.models.available_models[1].description
        picker_context.with_overrides.assert_called_once_with(
            current_provider="openai-codex",
            current_model="gpt-5.4",
            current_base_url="https://api.openai.com/v1",
        )
        build_payload.assert_called_once_with(
            picker_context,
            explicit_only=True,
            include_unconfigured=False,
            picker_hints=False,
            canonical_order=True,
            pricing=False,
            capabilities=False,
            refresh=False,
            probe_custom_providers=False,
            probe_current_custom_provider=False,
            max_models=ACP_MAX_MODELS_PER_PROVIDER,
        )



    @pytest.mark.asyncio
    async def test_available_commands_include_help(self, agent):
        help_cmd = next(
            (cmd for cmd in agent._available_commands() if cmd.name == "help"),
            None,
        )

        assert help_cmd is not None
        assert help_cmd.description == "List available commands"
        assert help_cmd.input is None


    def test_build_usage_update_for_zed_context_indicator(self, agent, mock_manager):
        state = mock_manager.create_session(cwd="/tmp")
        state.history = [{"role": "user", "content": "hello"}]
        state.agent.context_compressor = MagicMock(context_length=100_000)
        state.agent._cached_system_prompt = "system"
        state.agent.tools = [{"type": "function", "function": {"name": "demo"}}]

        with patch(
            "agent.model_metadata.estimate_request_tokens_rough",
            return_value=25_000,
        ):
            update = agent._build_usage_update(state)

        assert isinstance(update, UsageUpdate)
        assert update.session_update == "usage_update"
        assert update.size == 100_000
        assert update.used == 25_000




    @pytest.mark.asyncio
    async def test_load_session_not_found_returns_none(self, agent):
        resp = await agent.load_session(cwd="/tmp", session_id="bogus")
        assert resp is None






    @pytest.mark.asyncio
    async def test_resume_session_replays_persisted_history_to_client(self, agent):
        mock_conn = MagicMock(spec=acp.Client)
        mock_conn.session_update = AsyncMock()
        agent._conn = mock_conn

        new_resp = await agent.new_session(cwd="/tmp")
        state = agent.session_manager.get_session(new_resp.session_id)
        state.history = [{"role": "user", "content": "So tell me the current state"}]

        mock_conn.session_update.reset_mock()
        resp = await agent.resume_session(cwd="/tmp", session_id=new_resp.session_id)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert isinstance(resp, ResumeSessionResponse)
        updates = [call.kwargs["update"] for call in mock_conn.session_update.await_args_list]
        assert any(
            isinstance(update, UserMessageChunk)
            and update.content.text == "So tell me the current state"
            for update in updates
        )











# ---------------------------------------------------------------------------
# list / fork
# ---------------------------------------------------------------------------


class TestListAndFork:
    @pytest.mark.asyncio
    async def test_fork_session(self, agent):
        new_resp = await agent.new_session(cwd="/original")
        fork_resp = await agent.fork_session(cwd="/forked", session_id=new_resp.session_id)
        assert fork_resp.session_id
        assert fork_resp.session_id != new_resp.session_id

    @pytest.mark.asyncio
    async def test_list_sessions_includes_title_and_updated_at(self, agent):
        with patch.object(
            agent.session_manager,
            "list_sessions",
            return_value=[
                {
                    "session_id": "session-1",
                    "cwd": "/tmp/project",
                    "title": "Fix Zed session history",
                    "updated_at": 123.0,
                }
            ],
        ):
            resp = await agent.list_sessions(cwd="/tmp/project")

        assert isinstance(resp.sessions[0], SessionInfo)
        assert resp.sessions[0].title == "Fix Zed session history"
        assert resp.sessions[0].updated_at == "123.0"






# ---------------------------------------------------------------------------
# session configuration / model routing
# ---------------------------------------------------------------------------


class TestSessionConfiguration:

    @pytest.mark.asyncio
    async def test_router_accepts_stable_session_config_methods(self, agent):
        new_resp = await agent.new_session(cwd="/tmp")
        router = build_agent_router(agent)

        mode_result = await router(
            "session/set_mode",
            {"modeId": "accept_edits", "sessionId": new_resp.session_id},
            False,
        )
        config_result = await router(
            "session/set_config_option",
            {
                "configId": "approval_mode",
                "sessionId": new_resp.session_id,
                "value": "auto",
            },
            False,
        )

        assert mode_result == {}
        assert config_result["configOptions"] == []





# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------


class TestPrompt:
    @pytest.mark.asyncio
    async def test_prompt_returns_refusal_for_unknown_session(self, agent):
        prompt = [TextContentBlock(type="text", text="hello")]
        resp = await agent.prompt(prompt=prompt, session_id="nonexistent")
        assert isinstance(resp, PromptResponse)
        assert resp.stop_reason == "refusal"

    @pytest.mark.asyncio
    async def test_prompt_binds_session_id_into_subprocess_env(self, agent, mock_manager):
        """The ACP prompt path must bridge the session id into child subprocesses.

        Regression: ``set_session_vars`` was called with ``session_key`` only,
        leaving the ``HERMES_SESSION_ID`` ContextVar bound to the explicit ""
        default. Once the session-context machinery is engaged, that empty value
        is authoritative — so ``_make_run_env`` handed child subprocesses an
        empty ``HERMES_SESSION_ID`` instead of the session's own id.
        """
        from tools.environments.local import _make_run_env

        resp = await agent.new_session(cwd=".")
        state = mock_manager.get_session(resp.session_id)

        captured: dict[str, str | None] = {}

        def _run(*args, **kwargs):
            # Runs inside the session context copy set up by prompt().
            captured["child"] = _make_run_env({}).get("HERMES_SESSION_ID")
            return {"final_response": "ok", "messages": []}

        state.agent.run_conversation = _run
        state.agent.model = "test-model"
        state.agent.provider = "openrouter"

        mock_conn = MagicMock(spec=acp.Client)
        mock_conn.session_update = AsyncMock()
        agent._conn = mock_conn

        await agent.prompt(
            prompt=[TextContentBlock(type="text", text="hi")],
            session_id=resp.session_id,
        )

        assert captured.get("child") == resp.session_id

















# ---------------------------------------------------------------------------
# on_connect
# ---------------------------------------------------------------------------


class TestOnConnect:
    def test_on_connect_stores_client(self, agent):
        mock_conn = MagicMock(spec=acp.Client)
        agent.on_connect(mock_conn)
        assert agent._conn is mock_conn


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


class TestSlashCommands:
    """Test slash command dispatch in the ACP adapter."""

    def _make_state(self, mock_manager):
        state = mock_manager.create_session(cwd="/tmp")
        state.agent.model = "test-model"
        state.agent.provider = "openrouter"
        state.model = "test-model"
        return state

    def test_help_lists_commands(self, agent, mock_manager):
        state = self._make_state(mock_manager)
        result = agent._handle_slash_command("/help", state)
        assert result is not None
        assert "/help" in result
        assert "/model" in result
        assert "/tools" in result
        assert "/reset" in result

    def test_model_shows_current(self, agent, mock_manager):
        state = self._make_state(mock_manager)
        result = agent._handle_slash_command("/model", state)
        assert "test-model" in result





    def test_reset_clears_history(self, agent, mock_manager):
        state = self._make_state(mock_manager)
        state.history = [{"role": "user", "content": "hello"}]
        result = agent._handle_slash_command("/reset", state)
        assert "cleared" in result.lower()
        assert len(state.history) == 0




    def test_compact_compresses_context(self, agent, mock_manager):
        state = self._make_state(mock_manager)
        state.history = [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
            {"role": "assistant", "content": "four"},
        ]
        state.agent.compression_enabled = True
        state.agent._cached_system_prompt = "system"
        state.agent.tools = None
        original_session_db = object()
        state.agent._session_db = original_session_db

        def _compress_context(messages, system_prompt, *, approx_tokens, task_id, force):
            assert state.agent._session_db is None
            assert messages == state.history
            assert system_prompt == "system"
            assert approx_tokens == 40
            assert task_id == state.session_id
            assert force is True
            return [{"role": "user", "content": "summary"}], "new-system"

        state.agent._compress_context = MagicMock(side_effect=_compress_context)

        with (
            patch.object(agent.session_manager, "save_session") as mock_save,
            patch(
                "agent.model_metadata.estimate_request_tokens_rough",
                side_effect=[40, 12],
            ),
        ):
            result = agent._handle_slash_command("/compress", state)

        assert "Context compressed: 4 -> 1 messages" in result
        assert "~40 -> ~12 tokens" in result
        assert state.history == [{"role": "user", "content": "summary"}]
        assert state.agent._session_db is original_session_db
        state.agent._compress_context.assert_called_once_with(
            [
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "two"},
                {"role": "user", "content": "three"},
                {"role": "assistant", "content": "four"},
            ],
            "system",
            approx_tokens=40,
            task_id=state.session_id,
            force=True,
        )
        mock_save.assert_called_once_with(state.session_id)


    def test_unknown_command_returns_none(self, agent, mock_manager):
        state = self._make_state(mock_manager)
        result = agent._handle_slash_command("/nonexistent", state)
        assert result is None


    def test_slash_handler_cwd_pin_does_not_leak(self, agent, mock_manager, tmp_path):
        """The pin is scoped to the handler's own context copy.

        Concurrent ACP sessions share the event loop, so a handler that pinned
        the ambient context would leave its workspace bound for whatever runs
        next. Asserting the ambient value is unchanged after dispatch keeps the
        fix from trading one cross-session leak for another.
        """
        from agent.runtime_cwd import resolve_agent_cwd

        workspace = tmp_path / "project"
        workspace.mkdir()
        state = mock_manager.create_session(cwd=str(workspace))
        state.cwd = str(workspace)
        state.agent.model = "test-model"
        state.agent.provider = "openrouter"

        before = str(resolve_agent_cwd())
        agent._handle_slash_command("/help", state)
        assert str(resolve_agent_cwd()) == before





# ---------------------------------------------------------------------------
# _register_session_mcp_servers
# ---------------------------------------------------------------------------


class TestRegisterSessionMcpServers:
    """Tests for ACP MCP server registration in session lifecycle."""

    @pytest.mark.asyncio
    async def test_noop_when_no_servers(self, agent, mock_manager):
        """No-op when mcp_servers is None or empty."""
        state = mock_manager.create_session(cwd="/tmp")
        # Should not raise
        await agent._register_session_mcp_servers(state, None)
        await agent._register_session_mcp_servers(state, [])

    @pytest.mark.asyncio
    async def test_registers_stdio_servers(self, agent, mock_manager):
        """McpServerStdio servers are converted and acquired for the session."""
        from acp.schema import McpServerStdio, EnvVariable

        state = mock_manager.create_session(cwd="/tmp")
        # Give the mock agent the attributes _register_session_mcp_servers reads
        state.agent.enabled_toolsets = ["hermes-acp"]
        state.agent.disabled_toolsets = None
        state.agent.tools = []
        state.agent.valid_tool_names = set()

        server = McpServerStdio(
            name="test-server",
            command="/usr/bin/test",
            args=["--flag"],
            env=[EnvVariable(name="KEY", value="val")],
        )

        registered_config = {}
        def capture_register(owner_id, config_map):
            assert owner_id == state.session_id
            registered_config.update(config_map)
            return ["mcp_test_server_tool1"]

        with patch("tools.mcp_tool.acquire_session_mcp_servers", side_effect=capture_register), \
             patch("model_tools.get_tool_definitions", return_value=[]):
            await agent._register_session_mcp_servers(state, [server])

        assert "test-server" in registered_config
        cfg = registered_config["test-server"]
        assert cfg["command"] == "/usr/bin/test"
        assert cfg["args"] == ["--flag"]
        assert cfg["env"] == {"KEY": "val"}


    @pytest.mark.asyncio
    async def test_refreshes_agent_tool_surface(self, agent, mock_manager):
        """After MCP registration, agent.tools and valid_tool_names are refreshed."""
        from acp.schema import McpServerStdio

        state = mock_manager.create_session(cwd="/tmp")
        state.agent.enabled_toolsets = ["hermes-acp"]
        state.agent.disabled_toolsets = None
        state.agent.tools = []
        state.agent.valid_tool_names = set()
        state.agent._cached_system_prompt = "old prompt"
        state.agent._memory_manager = SimpleNamespace(
            get_all_tool_schemas=lambda: [
                {"name": "hindsight_recall", "description": "Recall", "parameters": {}}
            ]
        )

        server = McpServerStdio(
            name="srv",
            command="/bin/test",
            args=[],
            env=[],
        )

        fake_tools = [
            {"function": {"name": "mcp_srv_search"}},
            {"function": {"name": "memory"}},
            {"function": {"name": "terminal"}},
        ]

        with patch("tools.mcp_tool.acquire_session_mcp_servers", return_value=["mcp_srv_search"]), \
             patch("model_tools.get_tool_definitions", return_value=fake_tools) as mock_defs:
            await agent._register_session_mcp_servers(state, [server])

        mock_defs.assert_called_once_with(
            enabled_toolsets=["hermes-acp", "mcp-srv"],
            disabled_toolsets=None,
            quiet_mode=True,
        )
        assert state.agent.enabled_toolsets == ["hermes-acp", "mcp-srv"]
        assert state.agent.tools is fake_tools
        assert state.agent.tools[-1] == {
            "type": "function",
            "function": {
                "name": "hindsight_recall",
                "description": "Recall",
                "parameters": {},
            },
        }
        assert state.agent.valid_tool_names == {
            "hindsight_recall",
            "memory",
            "mcp_srv_search",
            "terminal",
        }
        # _invalidate_system_prompt should have been called
        state.agent._invalidate_system_prompt.assert_called_once()

    @pytest.mark.asyncio
    async def test_register_failure_fails_session_closed(self, agent, mock_manager):
        """A requested MCP server must connect before the session succeeds."""
        from acp.schema import McpServerStdio

        state = mock_manager.create_session(cwd="/tmp")
        server = McpServerStdio(
            name="bad",
            command="/nonexistent",
            args=[],
            env=[],
        )

        with patch("tools.mcp_tool.acquire_session_mcp_servers", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="MCP server registration failed"):
                await agent._register_session_mcp_servers(state, [server])


    @pytest.mark.asyncio
    async def test_initialize_advertises_http_mcp(self, agent):
        response = await agent.initialize()
        assert response.agent_capabilities.mcp_capabilities.http is True

    @pytest.mark.asyncio
    async def test_explicit_empty_list_releases_session_servers(self, agent, mock_manager):
        state = mock_manager.create_session(cwd="/tmp")
        state.mcp_server_names = {"attempt"}
        state.agent.enabled_toolsets = ["hermes-acp", "mcp-attempt"]
        state.agent.tools = [{"function": {"name": "mcp__attempt__ping"}}]
        state.agent.valid_tool_names = {"mcp__attempt__ping"}
        base_tools = [{"function": {"name": "terminal"}}]
        with patch("tools.mcp_tool.release_session_mcp_servers") as release, patch(
            "tools.mcp_tool.acquire_session_mcp_servers"
        ), patch("model_tools.get_tool_definitions", return_value=base_tools):
            await agent._register_session_mcp_servers(state, [])
        release.assert_called_once_with(state.session_id, names={"attempt"})
        assert state.mcp_server_names == set()
        assert state.agent.enabled_toolsets == ["hermes-acp"]
        assert state.agent.tools == base_tools
        assert state.agent.valid_tool_names == {"terminal"}

    @pytest.mark.asyncio
    async def test_http_descriptor_enforces_loopback_cleartext_and_redirect_safety(
        self, agent, mock_manager
    ):
        from acp.schema import HttpHeader, McpServerHttp

        state = mock_manager.create_session(cwd="/tmp")
        server = McpServerHttp(
            name="attempt",
            url="http://127.0.0.1:43123/mcp",
            headers=[HttpHeader(name="Authorization", value="Bearer secret")],
        )
        captured = {}
        def acquire(owner_id, config_map):
            captured.update(config_map)
            return []

        with patch("tools.mcp_tool.acquire_session_mcp_servers", side_effect=acquire), \
             patch("model_tools.get_tool_definitions", return_value=[]):
            await agent._register_session_mcp_servers(state, [server])
        assert captured["attempt"]["strict_redirect_headers"] is True
        assert captured["attempt"]["skip_preflight"] is True

    @pytest.mark.asyncio
    async def test_new_session_registration_failure_removes_session(self, agent, mock_manager):
        from acp.schema import McpServerStdio

        server = McpServerStdio(name="bad", command="/missing", args=[], env=[])
        with patch(
            "tools.mcp_tool.acquire_session_mcp_servers",
            side_effect=RuntimeError("secret transport detail"),
        ):
            with pytest.raises(RuntimeError, match="MCP server registration failed") as raised:
                await agent.new_session(cwd="/tmp", mcp_servers=[server])
        assert "secret transport detail" not in str(raised.value)
        assert mock_manager._sessions == {}

    @pytest.mark.asyncio
    async def test_close_releases_before_removing_session(self, agent, mock_manager):
        state = mock_manager.create_session(cwd="/tmp")
        state.mcp_server_names = {"attempt"}
        with patch("tools.mcp_tool.release_session_mcp_servers") as release:
            await agent.close_session(session_id=state.session_id)
        release.assert_called_once_with(state.session_id)
        assert mock_manager.get_session(state.session_id) is None

    @pytest.mark.asyncio
    async def test_close_release_failure_preserves_retryable_session(self, agent, mock_manager):
        state = mock_manager.create_session(cwd="/tmp")
        state.mcp_server_names = {"attempt"}
        with patch(
            "tools.mcp_tool.release_session_mcp_servers",
            side_effect=RuntimeError("secret shutdown detail"),
        ):
            with pytest.raises(RuntimeError, match="MCP server release failed") as raised:
                await agent.close_session(session_id=state.session_id)
        assert "secret shutdown detail" not in str(raised.value)
        assert mock_manager.get_session(state.session_id) is state

    @pytest.mark.asyncio
    async def test_close_waits_for_active_turn_before_release(self, agent, mock_manager):
        state = mock_manager.create_session(cwd="/tmp")
        state.mcp_server_names = {"attempt"}
        state.is_running = True
        order = []
        async def finish_turn():
            await asyncio.sleep(0.02)
            with state.runtime_lock:
                state.is_running = False
            order.append("turn-finished")
        def release(owner_id):
            order.append("released")

        finisher = asyncio.create_task(finish_turn())
        with patch("tools.mcp_tool.release_session_mcp_servers", side_effect=release):
            await agent.close_session(session_id=state.session_id)
        await finisher
        assert order == ["turn-finished", "released"]

    @pytest.mark.asyncio
    async def test_close_discards_queued_fallback_before_release(self, agent, mock_manager):
        state = mock_manager.create_session(cwd="/tmp")
        state.is_running = True
        state.queued_prompts = ["must-not-run"]
        async def finish_turn():
            await asyncio.sleep(0.02)
            with state.runtime_lock:
                state.is_running = False

        finisher = asyncio.create_task(finish_turn())
        with patch("tools.mcp_tool.release_session_mcp_servers"):
            await agent.close_session(session_id=state.session_id)
        await finisher
        assert state.queued_prompts == []
        assert state.closing is True

    @pytest.mark.asyncio
    async def test_invalid_http_header_never_reaches_error_or_log(
        self, agent, mock_manager, caplog
    ):
        from acp.schema import HttpHeader, McpServerHttp

        state = mock_manager.create_session(cwd="/tmp")
        secret = "Bearer secret-never-log"
        server = McpServerHttp(
            name="attempt",
            url="http://127.0.0.1:43123/mcp",
            headers=[HttpHeader(name="Authorization", value=f"{secret}\r\nInjected: yes")],
        )
        with pytest.raises(RuntimeError, match="MCP server registration failed") as raised:
            await agent._register_session_mcp_servers(state, [server])
        assert secret not in str(raised.value)
        assert secret not in caplog.text

    @pytest.mark.asyncio
    async def test_cleartext_http_rejects_non_loopback_endpoint(self, agent, mock_manager):
        from acp.schema import McpServerHttp

        state = mock_manager.create_session(cwd="/tmp")
        server = McpServerHttp(
            name="remote",
            url="http://example.test/mcp",
            headers=[],
        )
        with pytest.raises(RuntimeError, match="MCP server registration failed"):
            await agent._register_session_mcp_servers(state, [server])

    @pytest.mark.asyncio
    async def test_tool_surface_refresh_failure_releases_new_acquisition(
        self, agent, mock_manager
    ):
        from acp.schema import McpServerStdio

        state = mock_manager.create_session(cwd="/tmp")
        server = McpServerStdio(name="srv", command="/bin/test", args=[], env=[])
        with patch("tools.mcp_tool.acquire_session_mcp_servers"), patch(
            "tools.mcp_tool.release_session_mcp_servers"
        ) as release, patch(
            "model_tools.get_tool_definitions", side_effect=RuntimeError("refresh secret")
        ):
            with pytest.raises(RuntimeError, match="MCP tool refresh failed") as raised:
                await agent._register_session_mcp_servers(state, [server])
        assert "refresh secret" not in str(raised.value)
        release.assert_called_once_with(state.session_id, names={"srv"})
        assert state.mcp_server_names == set()

    @pytest.mark.asyncio
    async def test_failed_new_session_rollback_keeps_retryable_handle(
        self, agent, mock_manager
    ):
        from acp.schema import McpServerStdio
        server = McpServerStdio(name="srv", command="/bin/test", args=[], env=[])
        with patch("tools.mcp_tool.acquire_session_mcp_servers"), patch(
            "tools.mcp_tool.release_session_mcp_servers",
            side_effect=RuntimeError("secret release detail"),
        ), patch("model_tools.get_tool_definitions", side_effect=RuntimeError("refresh")):
            with pytest.raises(RuntimeError, match="retry close for session") as raised:
                await agent.new_session(cwd="/tmp", mcp_servers=[server])

        assert "secret release detail" not in str(raised.value)
        assert len(mock_manager._sessions) == 1
        state = next(iter(mock_manager._sessions.values()))
        assert state.session_id in str(raised.value)
        assert state.mcp_server_names == {"srv"}

    @pytest.mark.asyncio
    async def test_initialize_omits_http_when_strict_transport_is_unavailable(self, agent):
        with patch("tools.mcp_tool._MCP_HTTP_AVAILABLE", False), patch(
            "tools.mcp_tool._MCP_NEW_HTTP", False
        ):
            response = await agent.initialize()

        assert response.agent_capabilities.mcp_capabilities.http is False
