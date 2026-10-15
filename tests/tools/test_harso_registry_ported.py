"""Harso registry egress regression tests ported from the v0.21 cell fork."""
import json
import logging
from unittest.mock import patch
import pytest
from tools.registry import ToolRegistry, _MAX_LOGGED_ERROR_CHARS, _MAX_TOOL_ERROR_CHARS

def _make_schema(name="test_tool"):
    return {
        "name": name,
        "description": f"A {name}",
        "parameters": {"type": "object", "properties": {}},
    }

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

class TestVaultScrubRound2Residuals:
    """F3/F4 (#58 r2): the wire document is parsed before any raw-form precheck, string leaves and keys are
    scrubbed as text (never re-parsed as new documents), and an inline image exempts only its data URL."""

    @pytest.fixture(autouse=True)
    def clean(self):
        from agent import redact

        redact.clear_vault_redaction_values()
        yield
        redact.clear_vault_redaction_values()

    @staticmethod
    def send(secret, value):
        from agent import redact

        redact.register_vault_redaction_value(secret)
        reg = ToolRegistry()
        reg.register(name="probe", toolset="review", schema={}, handler=lambda args, **kw: value)
        return reg.dispatch("probe", {})

    @pytest.mark.parametrize("wire", ['{"value":"\\u0053ensitive42"}', '{"\\u0053ensitive42":"ok"}'])
    def test_json_representation_cannot_hide_registered_text(self, wire):
        out = json.loads(self.send("Sensitive42", wire))
        assert "Sensitive42" not in json.dumps(out)

    @pytest.mark.parametrize("secret,payload", [
        ("123456", {"body": '{"pin":123456}'}),
        ("123456", {"body": "[123456]"}),
        ('{"a":1}', {"body": '{"a":1}'}),
        ("123456", {"[123456]": "text"}),
    ])
    def test_string_leaves_and_keys_are_text_not_new_wire_documents(self, secret, payload):
        out = json.loads(self.send(secret, json.dumps(payload)))
        assert all(secret not in key and (not isinstance(value, str) or secret not in value)
                   for key, value in out.items())

    @pytest.mark.parametrize("detail", ["Sensitive42", {"value": "Sensitive42"}])
    def test_inline_image_only_bytes_are_exempt(self, detail):
        out = self.send("Sensitive42", {"_multimodal": True, "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD", "detail": detail}}]})
        assert "Sensitive42" not in json.dumps(out)
        assert out["content"][0]["image_url"]["url"] == "data:image/png;base64,QUJD"

    def test_real_numeric_values_remain_numbers(self):
        out = json.loads(self.send("123456", json.dumps({"pin": 123456, "note": "123456"})))
        assert out["pin"] == 123456
        assert out["note"] == "«redacted-vault-secret»"

    def test_a_json_result_without_the_value_is_byte_identical(self):
        wire = '{ "a" : "\\u0041", "n": 1.50, "b": true }'  # spacing, escape and number spelling kept
        assert self.send("Sensitive42", wire) == wire
