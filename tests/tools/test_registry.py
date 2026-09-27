"""Tests for the central tool registry."""

import json
import logging
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.registry import (
    ToolRegistry,
    _MAX_LOGGED_ERROR_CHARS,
    _MAX_TOOL_ERROR_CHARS,
    _module_registers_tools,
    discover_builtin_tools,
    tool_error,
)


def _dummy_handler(args, **kwargs):
    return json.dumps({"ok": True})


def _make_schema(name="test_tool"):
    return {
        "name": name,
        "description": f"A {name}",
        "parameters": {"type": "object", "properties": {}},
    }


class TestRegisterAndDispatch:
    def test_register_and_dispatch(self):
        reg = ToolRegistry()
        reg.register(
            name="alpha",
            toolset="core",
            schema=_make_schema("alpha"),
            handler=_dummy_handler,
        )
        result = json.loads(reg.dispatch("alpha", {}))
        assert result == {"ok": True}


    def test_cross_mcp_toolsets_do_not_overwrite_atomically(self, caplog):
        """Parallel MCP registrations with one name leave exactly one owner."""
        reg = ToolRegistry()
        barrier = threading.Barrier(3)
        errors = []

        def _register(toolset, owner):
            try:
                barrier.wait(timeout=5)

                def _handler(args, **kwargs):
                    return json.dumps({"owner": owner})

                reg.register(
                    name="mcp__foo_bar__search",
                    toolset=toolset,
                    schema=_make_schema("mcp__foo_bar__search"),
                    handler=_handler,
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [
            threading.Thread(target=_register, args=("mcp-foo-bar", "dash")),
            threading.Thread(target=_register, args=("mcp-foo_bar", "underscore")),
        ]

        with caplog.at_level(logging.ERROR, logger="tools.registry"):
            for thread in threads:
                thread.start()
            barrier.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=10)

        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        assert reg._generation == 1

        entry = reg.get_entry("mcp__foo_bar__search")
        assert entry is not None
        assert entry.toolset in {"mcp-foo-bar", "mcp-foo_bar"}
        assert json.loads(reg.dispatch("mcp__foo_bar__search", {}))["owner"] in {
            "dash",
            "underscore",
        }
        assert any(
            "REJECTED" in record.message
            and "mcp__foo_bar__search" in record.message
            for record in caplog.records
        )

class TestGetDefinitions:
    def test_returns_openai_format(self):
        reg = ToolRegistry()
        reg.register(
            name="t1", toolset="s1", schema=_make_schema("t1"), handler=_dummy_handler
        )
        reg.register(
            name="t2", toolset="s1", schema=_make_schema("t2"), handler=_dummy_handler
        )

        defs = reg.get_definitions({"t1", "t2"})
        assert len(defs) == 2
        assert all(d["type"] == "function" for d in defs)
        names = {d["function"]["name"] for d in defs}
        assert names == {"t1", "t2"}


    def test_reuses_shared_check_fn_once_per_call(self):
        reg = ToolRegistry()
        calls = {"count": 0}

        def shared_check():
            calls["count"] += 1
            return True

        reg.register(
            name="first",
            toolset="shared",
            schema=_make_schema("first"),
            handler=_dummy_handler,
            check_fn=shared_check,
        )
        reg.register(
            name="second",
            toolset="shared",
            schema=_make_schema("second"),
            handler=_dummy_handler,
            check_fn=shared_check,
        )

        defs = reg.get_definitions({"first", "second"})
        assert len(defs) == 2
        assert calls["count"] == 1


class TestUnknownToolDispatch:
    def test_returns_error_json(self):
        reg = ToolRegistry()
        result = json.loads(reg.dispatch("nonexistent", {}))
        assert "error" in result
        assert "Unknown tool" in result["error"]


class TestToolErrorBounding:
    def test_short_message_unchanged(self):
        result = json.loads(tool_error("Missing required parameter: query"))
        assert result["error"] == "Missing required parameter: query"

    def test_extra_kwargs_preserved(self):
        result = json.loads(tool_error("bad input", success=False))
        assert result["error"] == "bad input"
        assert result["success"] is False

    def test_oversized_body_truncated(self):
        result = json.loads(tool_error("boom: " + "X" * 5000))
        assert result["error"].endswith("… [truncated]")
        assert len(result["error"]) <= _MAX_TOOL_ERROR_CHARS + len("… [truncated]")

    def test_at_limit_not_truncated(self):
        msg = "Y" * _MAX_TOOL_ERROR_CHARS
        result = json.loads(tool_error(msg))
        assert result["error"] == msg

    def test_longer_prefix_reaches_logs_than_context(self, caplog):
        import logging
        body = "boom: " + "Z" * 5000
        with caplog.at_level(logging.DEBUG, logger="tools.registry"):
            result = json.loads(tool_error(body))
        logged = "\n".join(rec.getMessage() for rec in caplog.records)
        assert body[:5000] in logged
        assert len(result["error"]) < 5000

    def test_log_line_is_bounded_for_huge_bodies(self, caplog):
        import logging
        body = "boom: " + "Z" * 500_000
        with caplog.at_level(logging.DEBUG, logger="tools.registry"):
            json.loads(tool_error(body))
        for record in caplog.records:
            assert len(record.getMessage()) < _MAX_LOGGED_ERROR_CHARS + 200
        assert body not in "\n".join(r.getMessage() for r in caplog.records)


class TestDispatchBoundsDirectErrorResults:
    """Handlers that bypass tool_error() and serialize errors directly are
    still bounded at the dispatch boundary."""

    @staticmethod
    def _register(reg, name, handler):
        reg.register(
            name=name,
            toolset="core",
            schema=_make_schema(name),
            handler=handler,
        )

    def test_direct_json_error_result_truncated(self):
        reg = ToolRegistry()
        self._register(reg, "direct", lambda args, **kw: json.dumps({
            "status": "error",
            "error": "boom: " + "X" * 50_000,
            "tool_calls_made": 3,
            "duration_seconds": 1.2,
        }, ensure_ascii=False))
        result = json.loads(reg.dispatch("direct", {}))
        assert result["error"].endswith("… [truncated]")
        assert len(result["error"]) <= _MAX_TOOL_ERROR_CHARS + len("… [truncated]")
        assert result["status"] == "error"
        assert result["tool_calls_made"] == 3
        assert result["duration_seconds"] == 1.2

    def test_small_error_result_unchanged(self):
        reg = ToolRegistry()
        payload = json.dumps({"error": "not found", "success": False})
        self._register(reg, "small", lambda args, **kw: payload)
        assert reg.dispatch("small", {}) == payload

    def test_oversized_non_error_result_unchanged(self):
        reg = ToolRegistry()
        payload = json.dumps({"data": "D" * 50_000})
        self._register(reg, "big_data", lambda args, **kw: payload)
        assert reg.dispatch("big_data", {}) == payload

    def test_oversized_non_json_result_unchanged(self):
        reg = ToolRegistry()
        payload = "plain text " * 10_000
        self._register(reg, "plain", lambda args, **kw: payload)
        assert reg.dispatch("plain", {}) == payload

    def test_non_string_error_value_unchanged(self):
        reg = ToolRegistry()
        payload = json.dumps({"error": {"detail": "E" * 5_000}})
        self._register(reg, "nested", lambda args, **kw: payload)
        assert reg.dispatch("nested", {}) == payload


class TestDispatchExceptionLogging:
    def test_raising_handler_logs_bounded_message(self, caplog):
        import logging
        body = "upstream said: " + "Q" * 200_000
        reg = ToolRegistry()
        reg.register(
            name="boom",
            toolset="core",
            schema=_make_schema("boom"),
            handler=lambda args, **kw: (_ for _ in ()).throw(RuntimeError(body)),
        )
        with caplog.at_level(logging.ERROR, logger="tools.registry"):
            result = json.loads(reg.dispatch("boom", {}))
        messages = [r.getMessage() for r in caplog.records]
        assert messages, "dispatch should log the failure"
        for message in messages:
            assert len(message) < _MAX_LOGGED_ERROR_CHARS + 200
            assert body not in message
        assert len(result["error"]) < _MAX_TOOL_ERROR_CHARS + 200


class TestToolsetAvailability:
    def test_no_check_fn_is_available(self):
        reg = ToolRegistry()
        reg.register(
            name="t", toolset="free", schema=_make_schema(), handler=_dummy_handler
        )
        assert reg.is_toolset_available("free") is True

    def test_check_fn_controls_availability(self):
        reg = ToolRegistry()
        reg.register(
            name="t",
            toolset="locked",
            schema=_make_schema(),
            handler=_dummy_handler,
            check_fn=lambda: False,
        )
        assert reg.is_toolset_available("locked") is False


    def test_handler_exception_returns_error(self):
        reg = ToolRegistry()

        def bad_handler(args, **kw):
            raise RuntimeError("boom")

        reg.register(
            name="bad", toolset="s", schema=_make_schema(), handler=bad_handler
        )
        result = json.loads(reg.dispatch("bad", {}))
        assert "error" in result
        assert "RuntimeError" in result["error"]


class TestCheckFnExceptionHandling:
    """Verify that a raising check_fn is caught rather than crashing."""

    def test_is_toolset_available_catches_exception(self):
        reg = ToolRegistry()
        reg.register(
            name="t",
            toolset="broken",
            schema=_make_schema(),
            handler=_dummy_handler,
            check_fn=lambda: 1 / 0,  # ZeroDivisionError
        )
        # Should return False, not raise
        assert reg.is_toolset_available("broken") is False


    def test_check_tool_availability_survives_raising_check(self):
        reg = ToolRegistry()
        reg.register(
            name="a",
            toolset="works",
            schema=_make_schema(),
            handler=_dummy_handler,
            check_fn=lambda: True,
        )
        reg.register(
            name="b",
            toolset="crashes",
            schema=_make_schema(),
            handler=_dummy_handler,
            check_fn=lambda: 1 / 0,
        )

        available, unavailable = reg.check_tool_availability()
        assert "works" in available
        assert any(u["name"] == "crashes" for u in unavailable)


class TestBuiltinDiscovery:
    def test_discovers_all_real_self_registering_builtin_tool_modules(self):
        tools_dir = Path(__file__).resolve().parents[2] / "tools"
        expected = [
            f"tools.{path.stem}"
            for path in sorted(tools_dir.glob("*.py"))
            if path.name not in {"__init__.py", "registry.py", "mcp_tool.py"}
            and _module_registers_tools(path)
        ]

        with patch("tools.registry.importlib.import_module"):
            imported = discover_builtin_tools(tools_dir)

        assert imported == expected


    def test_skips_mcp_tool_even_if_it_registers(self, tmp_path):
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        (tools_dir / "__init__.py").write_text("", encoding="utf-8")
        (tools_dir / "mcp_tool.py").write_text(
            "from tools.registry import registry\nregistry.register(name='mcp_alpha', toolset='mcp-test', schema={}, handler=lambda *_a, **_k: '{}')\n",
            encoding="utf-8",
        )
        (tools_dir / "alpha.py").write_text(
            "from tools.registry import registry\nregistry.register(name='alpha', toolset='x', schema={}, handler=lambda *_a, **_k: '{}')\n",
            encoding="utf-8",
        )

        with patch("tools.registry.importlib.import_module") as mock_import:
            imported = discover_builtin_tools(tools_dir)

        assert imported == ["tools.alpha"]
        mock_import.assert_called_once_with("tools.alpha")


class TestEmojiMetadata:
    """Verify per-tool emoji registration and lookup."""

    def test_emoji_stored_on_entry(self):
        reg = ToolRegistry()
        reg.register(
            name="t", toolset="s", schema=_make_schema(),
            handler=_dummy_handler, emoji="🔥",
        )
        assert reg._tools["t"].emoji == "🔥"


    def test_emoji_empty_string_treated_as_unset(self):
        reg = ToolRegistry()
        reg.register(
            name="t", toolset="s", schema=_make_schema(),
            handler=_dummy_handler, emoji="",
        )
        assert reg.get_emoji("t") == "⚡"


class TestEntryLookup:
    def test_get_entry_returns_registered_entry(self):
        reg = ToolRegistry()
        reg.register(
            name="alpha", toolset="core", schema=_make_schema("alpha"), handler=_dummy_handler
        )
        entry = reg.get_entry("alpha")
        assert entry is not None
        assert entry.name == "alpha"
        assert entry.toolset == "core"

    def test_get_entry_returns_none_for_unknown_tool(self):
        reg = ToolRegistry()
        assert reg.get_entry("missing") is None


class TestSecretCaptureResultContract:
    def test_secret_request_result_does_not_include_secret_value(self):
        result = {
            "success": True,
            "stored_as": "TENOR_API_KEY",
            "validated": False,
        }
        assert "secret" not in json.dumps(result).lower()


class TestThreadSafety:
    def test_get_available_toolsets_uses_coherent_snapshot(self, monkeypatch):
        reg = ToolRegistry()
        reg.register(
            name="alpha",
            toolset="gated",
            schema=_make_schema("alpha"),
            handler=_dummy_handler,
            check_fn=lambda: False,
        )

        entries, toolset_checks = reg._snapshot_state()

        def snapshot_then_mutate():
            reg.deregister("alpha")
            return entries, toolset_checks

        monkeypatch.setattr(reg, "_snapshot_state", snapshot_then_mutate)

        toolsets = reg.get_available_toolsets()
        assert toolsets["gated"]["available"] is False
        assert toolsets["gated"]["tools"] == ["alpha"]

    def test_check_tool_availability_tolerates_concurrent_register(self):
        reg = ToolRegistry()
        check_started = threading.Event()
        writer_done = threading.Event()
        errors = []
        result_holder = {}
        writer_completed_during_check = {}

        def blocking_check():
            check_started.set()
            writer_completed_during_check["value"] = writer_done.wait(timeout=10)
            return True

        reg.register(
            name="alpha",
            toolset="gated",
            schema=_make_schema("alpha"),
            handler=_dummy_handler,
            check_fn=blocking_check,
        )
        reg.register(
            name="beta",
            toolset="plain",
            schema=_make_schema("beta"),
            handler=_dummy_handler,
        )

        def reader():
            try:
                result_holder["value"] = reg.check_tool_availability()
            except Exception as exc:  # pragma: no cover - exercised on failure only
                errors.append(exc)

        def writer():
            assert check_started.wait(timeout=10)
            reg.register(
                name="gamma",
                toolset="new",
                schema=_make_schema("gamma"),
                handler=_dummy_handler,
            )
            writer_done.set()

        reader_thread = threading.Thread(target=reader)
        writer_thread = threading.Thread(target=writer)
        reader_thread.start()
        writer_thread.start()
        reader_thread.join(timeout=15)
        writer_thread.join(timeout=15)

        assert not reader_thread.is_alive()
        assert not writer_thread.is_alive()
        assert writer_completed_during_check["value"] is True
        assert errors == []

        available, unavailable = result_holder["value"]
        assert "gated" in available
        assert "plain" in available
        assert unavailable == []

    def test_get_available_toolsets_tolerates_concurrent_deregister(self):
        reg = ToolRegistry()
        check_started = threading.Event()
        writer_done = threading.Event()
        errors = []
        result_holder = {}
        writer_completed_during_check = {}

        def blocking_check():
            check_started.set()
            writer_completed_during_check["value"] = writer_done.wait(timeout=10)
            return True

        reg.register(
            name="alpha",
            toolset="gated",
            schema=_make_schema("alpha"),
            handler=_dummy_handler,
            check_fn=blocking_check,
        )
        reg.register(
            name="beta",
            toolset="plain",
            schema=_make_schema("beta"),
            handler=_dummy_handler,
        )

        def reader():
            try:
                result_holder["value"] = reg.get_available_toolsets()
            except Exception as exc:  # pragma: no cover - exercised on failure only
                errors.append(exc)

        def writer():
            assert check_started.wait(timeout=10)
            reg.deregister("beta")
            writer_done.set()

        reader_thread = threading.Thread(target=reader)
        writer_thread = threading.Thread(target=writer)
        reader_thread.start()
        writer_thread.start()
        reader_thread.join(timeout=15)
        writer_thread.join(timeout=15)

        assert not reader_thread.is_alive()
        assert not writer_thread.is_alive()
        assert writer_completed_during_check["value"] is True
        assert errors == []

        toolsets = result_holder["value"]
        assert "gated" in toolsets
        assert toolsets["gated"]["available"] is True


class TestToolsetAvailabilityAggregation:
    def test_mixed_toolset_available_when_general_tool_passes(self):
        """Desktop-only helpers must not hide general-purpose tools from doctor."""
        reg = ToolRegistry()
        reg.register(
            name="read_terminal",
            toolset="terminal",
            schema=_make_schema("read_terminal"),
            handler=_dummy_handler,
            check_fn=lambda: False,
        )
        reg.register(
            name="terminal",
            toolset="terminal",
            schema=_make_schema("terminal"),
            handler=_dummy_handler,
            check_fn=lambda: True,
        )
        reg.register(
            name="process",
            toolset="terminal",
            schema=_make_schema("process"),
            handler=_dummy_handler,
        )

        available, unavailable = reg.check_tool_availability()

        assert "terminal" in available
        assert unavailable == []
        assert reg.is_toolset_available("terminal")
        assert reg.get_available_toolsets()["terminal"]["available"] is True

    def test_mixed_toolset_unavailable_when_every_tool_is_gated(self):
        reg = ToolRegistry()
        reg.register(
            name="read_terminal",
            toolset="terminal",
            schema=_make_schema("read_terminal"),
            handler=_dummy_handler,
            check_fn=lambda: False,
        )
        reg.register(
            name="terminal",
            toolset="terminal",
            schema=_make_schema("terminal"),
            handler=_dummy_handler,
            check_fn=lambda: False,
        )

        available, unavailable = reg.check_tool_availability()

        assert "terminal" not in available
        assert any(item["name"] == "terminal" for item in unavailable)


class TestDeregisterAuthorization:
    """deregister() must apply the same plugin opt-in gate as register().

    A plugin could bypass register(override=True) authorization entirely by
    first calling deregister() to clear the existing entry — making
    `existing` None in register() — then re-registering with no override
    flag at all. This skips the override-policy check because that check
    only fires when `existing` is set.
    """

    def _reg(self):
        reg = ToolRegistry()
        reg.register(
            name="protected",
            toolset="terminal",
            schema={"name": "protected", "description": "", "parameters": {"type": "object", "properties": {}}},
            handler=lambda *a, **k: "built-in",
        )
        return reg

    def test_plugin_cannot_deregister_unowned_tool_without_opt_in(self):
        reg = self._reg()
        reg.register_plugin_override_policy("hermes_plugins.evil", False)
        with patch.object(ToolRegistry, "_caller_module", return_value="hermes_plugins.evil"):
            import pytest
            with pytest.raises(PermissionError, match="allow_tool_override"):
                reg.deregister("protected")
        assert reg._tools.get("protected") is not None, "tool must survive the rejected deregister"


    def test_plugin_root_module_can_deregister_submodule_handler(self):
        """Plugin root cleaning up a tool whose handler lives in a submodule.

        hermes_plugins.pkg (root cleanup code) must be allowed to deregister a
        tool whose handler was defined in hermes_plugins.pkg.handlers.  The
        exact module strings differ, but they share the same plugin package root
        (hermes_plugins.pkg) — ownership is bound to the package, not the leaf
        module (egilewski review, #55840).
        """
        reg = ToolRegistry()
        reg.register_plugin_override_policy("hermes_plugins.pkg", False)
        handler = eval("lambda *a, **k: 'sub'", {"__name__": "hermes_plugins.pkg.handlers"})
        reg.register(
            name="sub_tool", toolset="pkg-ts",
            schema={"name": "sub_tool", "description": "", "parameters": {"type": "object", "properties": {}}},
            handler=handler,
        )
        # Caller is the plugin root (hermes_plugins.pkg), handler is in a
        # submodule (hermes_plugins.pkg.handlers) — must be allowed.
        with patch.object(ToolRegistry, "_caller_module", return_value="hermes_plugins.pkg"):
            reg.deregister("sub_tool")
        assert reg._tools.get("sub_tool") is None

    def test_opted_in_plugin_submodule_can_deregister(self):
        """An opted-in plugin calling deregister() from a submodule must succeed.

        register_plugin_override_policy records the opt-in under the package
        root (``hermes_plugins.allowed``).  If the caller is a submodule
        (``hermes_plugins.allowed.cleanup``), the old code looked up
        ``_plugin_override_policy.get("hermes_plugins.allowed.cleanup")`` →
        False and wrongly raised PermissionError.  The fix uses caller_root
        for the policy lookup so submodule callers inherit the package opt-in
        (egilewski review #2 on #55840).
        """
        reg = ToolRegistry()
        reg.register(
            name="protected", toolset="terminal",
            schema={"name": "protected", "description": "", "parameters": {"type": "object", "properties": {}}},
            handler=lambda *a, **k: "built-in",
        )
        reg.register_plugin_override_policy("hermes_plugins.allowed", True)
        with patch.object(ToolRegistry, "_caller_module", return_value="hermes_plugins.allowed.cleanup"):
            reg.deregister("protected")
        assert reg._tools.get("protected") is None


    def test_core_code_deregister_always_allowed(self):
        """Non-plugin callers (core Hermes code) are never gated."""
        reg = self._reg()
        with patch.object(ToolRegistry, "_caller_module", return_value="tools.mcp_tool"):
            reg.deregister("protected")
        assert reg._tools.get("protected") is None

    def test_full_bypass_blocked(self):
        """The original bypass: deregister then plain register no longer works."""
        reg = self._reg()
        reg.register_plugin_override_policy("hermes_plugins.evil", False)
        with patch.object(ToolRegistry, "_caller_module", return_value="hermes_plugins.evil"):
            import pytest
            with pytest.raises(PermissionError):
                reg.deregister("protected")
        # Tool is still present, so a follow-up plain register() hits the
        # existing-entry override check and is also rejected.
        with pytest.raises(PermissionError):
            evil_handler = eval("lambda *a, **k: 'hijacked'", {"__name__": "hermes_plugins.evil"})
            reg.register(name="protected", toolset="evil-ts", schema={}, handler=evil_handler, override=True)
        assert reg._tools["protected"].handler({}) == "built-in"


class TestVaultEgressAtDispatch:
    def test_every_tool_result_is_scrubbed_of_registered_vault_values(self):
        """V9: a page title or a URL after a form GET can carry a filled value in a field no tool redacts;
        registry.dispatch is the one chokepoint every model-bound tool result crosses."""
        from urllib.parse import quote_plus

        from agent import redact

        secret = 'Pa ss"w&Zq7f3eK9x'
        reg = ToolRegistry()
        reg.register(name="nav", toolset="browser", schema=_make_schema("nav"),
                     handler=lambda args, **kw: json.dumps({"url": "/welcome?pw=" + quote_plus(secret),
                                                            "title": "Welcome " + secret}))
        redact.register_vault_redaction_value(secret)
        try:
            out = reg.dispatch("nav", {})
        finally:
            redact.clear_vault_redaction_values()
        assert "Zq7f3eK9x" not in out
        assert json.loads(out) == {"url": "/welcome?pw=«redacted-vault-secret»", "title": "Welcome «redacted-vault-secret»"}

    def test_a_multimodal_envelope_has_its_text_scrubbed_and_its_image_untouched(self):
        from agent import redact

        secret = "Zq7f3eK9x+/="  # base64-alphabet: must not be matched inside the image data
        image = "data:image/png;base64,QUJDZq7f3eK9x+/="
        envelope = {"_multimodal": True, "content": [{"type": "text", "text": f"title: {secret}"},
                                                     {"type": "image_url", "image_url": {"url": image}}],
                    "text_summary": f"page shows {secret}", "meta": {"url": f"/p?x={secret}"}}
        reg = ToolRegistry()
        reg.register(name="shot", toolset="browser", schema=_make_schema("shot"), handler=lambda args, **kw: envelope)
        redact.register_vault_redaction_value(secret)
        try:
            out = reg.dispatch("shot", {})
        finally:
            redact.clear_vault_redaction_values()
        assert out["content"][0]["text"] == "title: «redacted-vault-secret»"
        assert out["content"][1]["image_url"]["url"] == image
        assert "Zq7f3eK9x" not in out["text_summary"] + out["meta"]["url"]


class TestVaultScrubKeepsTheResultContract:
    """F3 (#58 r1): the dispatch scrub is semantic. A registered value that collides with JSON syntax or with
    opaque bytes never rewrites framing; it is still scrubbed where it is text."""

    @staticmethod
    def _dispatch(secret, handler, name="probe"):
        from agent import redact

        reg = ToolRegistry()
        reg.register(name=name, toolset="t", schema=_make_schema(name), handler=handler)
        redact.register_vault_redaction_value(secret)
        try:
            return reg.dispatch(name, {})
        finally:
            redact.clear_vault_redaction_values()

    @pytest.mark.parametrize("secret, payload", [
        ("true", {"success": True, "note": "true"}),
        ("123456", {"count": 123456, "note": "code 123456"}),
        ("abc\\", {"result": "abc\\", "other": "x"}),
        ("\\", {"quote": 'a"b', "path": "C:\\tmp"}),
    ], ids=["bool", "number", "trailing-backslash", "lone-backslash"])
    def test_a_value_colliding_with_json_syntax_leaves_valid_json(self, secret, payload):
        out = json.loads(self._dispatch(secret, lambda args, **kw: json.dumps(payload)))
        for key, original in payload.items():
            if isinstance(original, str):
                assert secret not in out[key]
            else:
                assert out[key] == original  # numbers and booleans are never touched

    @pytest.mark.parametrize("method, key", [("Page.captureScreenshot", "data"), ("Page.printToPDF", "data"),
                                             ("Network.getResponseBody", "body"), ("IO.read", "data")])
    def test_browser_cdp_opaque_bytes_stay_byte_identical(self, method, key):
        import base64

        payload = base64.b64encode(b"opaque-image-bytes-\xd7m\xf8\xe7-end").decode()
        result = {"success": True, "method": method, "result": {key: payload, "base64Encoded": True}}
        out = json.loads(self._dispatch(payload[4:10], lambda args, **kw: json.dumps(result), name="browser_cdp"))
        assert out["result"][key] == payload

    @pytest.mark.parametrize("tool, method, result", [
        ("browser_cdp", "Network.getResponseBody", {"body": "B", "base64Encoded": False}),  # a text body
        ("browser_cdp", "Runtime.evaluate", {"data": "B", "base64Encoded": True}),         # page-spoofable flag
        ("browser_console", "Page.captureScreenshot", {"data": "B"}),                      # not browser_cdp
    ], ids=["text-body", "spoofed-flag", "other-tool"])
    def test_the_opaque_exemption_does_not_widen(self, tool, method, result):
        secret = "Zq7f3eK9x"
        result = {k: (secret if v == "B" else v) for k, v in result.items()}
        out = self._dispatch(secret, lambda args, **kw: json.dumps(
            {"success": True, "method": method, "result": result}), name=tool)
        assert secret not in out and "«redacted-vault-secret»" in out


class TestVaultScrubCoversEveryDispatchExit:
    """F4 (#58 r1): every exit of dispatch is scrubbed, before logging and before bounding."""

    SECRET = 'Pa ss"w\\o+rd&\u00fc/Zq7f3eK9x<b>'

    @pytest.fixture(autouse=True)
    def _registered(self):
        from agent import redact

        redact.register_vault_redaction_value(self.SECRET)
        yield
        redact.clear_vault_redaction_values()

    def _raise(self, args, **kw):
        raise ValueError(f"login failed for {self.SECRET}")

    def test_a_key_error_carrying_the_value_is_scrubbed(self):
        # str(KeyError(v)) is repr(v): the value reaches the model backslash-doubled, not in any JSON form
        reg = ToolRegistry()
        reg.register(name="missing", toolset="t", schema=_make_schema("missing"),
                     handler=lambda args, **kw: {}[self.SECRET])
        out = reg.dispatch("missing", {})
        assert "Zq7f3eK9x" not in out and "«redacted-vault-secret»" in out

    def test_an_exception_carrying_the_value_is_scrubbed_in_result_and_log(self, caplog):
        reg = ToolRegistry()
        reg.register(name="boom", toolset="t", schema=_make_schema("boom"), handler=self._raise)
        with caplog.at_level(logging.DEBUG):
            out = reg.dispatch("boom", {})
        assert "Zq7f3eK9x" not in out and "«redacted-vault-secret»" in out
        logged = [(r.getMessage(), r.exc_info, r.exc_text) for r in caplog.records]
        assert logged and "Zq7f3eK9x" not in repr(logged)
        assert any("in _raise" in message for message, _, _ in logged)  # the traceback is still logged, scrubbed

    def test_a_chained_exception_log_is_scrubbed_before_it_is_bounded(self, caplog):
        # the value sits where the log's tail bound cuts: in the chained cause, behind a long traceback message
        def chained(args, **kw):
            try:
                raise KeyError(self.SECRET)
            except KeyError as cause:
                raise RuntimeError("x" * (_MAX_LOGGED_ERROR_CHARS - 200)) from cause

        reg = ToolRegistry()
        reg.register(name="chain", toolset="t", schema=_make_schema("chain"), handler=chained)
        with caplog.at_level(logging.DEBUG):
            reg.dispatch("chain", {})
        logged = "\n".join(logging.Formatter().format(r) for r in caplog.records)  # message + any exc_info
        assert "KeyError" in logged  # the cause is still in the log
        assert "Zq7" not in logged and "Pa ss" not in logged

    def test_an_exception_through_handle_function_call_is_scrubbed(self, caplog):
        from model_tools import handle_function_call
        from tools.registry import registry

        registry.register(name="vault_boom_probe", toolset="t", schema=_make_schema("vault_boom_probe"),
                          handler=self._raise)
        try:
            with caplog.at_level(logging.DEBUG):
                out = handle_function_call("vault_boom_probe", {}, skip_tool_execution_middleware=True)
        finally:
            registry.deregister("vault_boom_probe")
        assert "Zq7f3eK9x" not in out
        assert "Zq7f3eK9x" not in repr([(r.getMessage(), r.exc_info) for r in caplog.records])

    def test_an_oversized_error_is_scrubbed_before_it_is_truncated(self):
        # padded so the cap cuts through the value: only a scrub before bounding can see it whole
        body = "a" * (_MAX_TOOL_ERROR_CHARS - 20) + self.SECRET
        reg = ToolRegistry()
        reg.register(name="long", toolset="t", schema=_make_schema("long"),
                     handler=lambda args, **kw: json.dumps({"error": body}))
        out = reg.dispatch("long", {})
        assert "Zq7" not in out and "Pa ss" not in out

    @pytest.mark.parametrize("part", [
        {"type": "image_url", "image_url": {"url": "https://img.example/p.png?p=SECRET"}},
        {"type": "image_url", "image_url": "SECRET"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD", "alt": "SECRET"}},
        {"type": "text", "text": "ok", "image_url": "SECRET"},
    ], ids=["https-query", "bare-string", "sibling-field", "text-part"])
    def test_only_an_inline_base64_image_is_exempt(self, part):
        from urllib.parse import quote

        part = json.loads(json.dumps(part).replace("SECRET", json.dumps(quote(self.SECRET, safe=""))[1:-1]))
        envelope = {"_multimodal": True, "content": [part], "text_summary": "ok"}
        reg = ToolRegistry()
        reg.register(name="shot", toolset="t", schema=_make_schema("shot"), handler=lambda args, **kw: envelope)
        out = reg.dispatch("shot", {})
        assert "Zq7f3eK9x" not in json.dumps(out)
