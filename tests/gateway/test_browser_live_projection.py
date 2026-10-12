from types import SimpleNamespace
from unittest.mock import Mock

from gateway.native_bot_admission import Projection


def test_native_tool_identity_survives_projection():
    adapter = SimpleNamespace(_native_submit_event=Mock())
    projection = Projection(adapter, "general", "session", None)
    projection.ref = "ref"
    for kind, expected in [("tool.start", "tool.started"), ("tool.complete", "tool.completed")]:
        projection._event("ref", kind, {"tool_id": "native-call-1", "name": "browser", "result": "ok"})
        adapter._native_submit_event.assert_called_with(
            "ref", expected, tool_call_id="native-call-1", tool_name="browser")


def test_native_http_activity_survives_disabled_verbose_progress(monkeypatch):
    from tui_gateway import server
    from gateway.native_bot_admission import Projection
    monkeypatch.setattr(server, "_sessions", {"session": {"transport": Projection(None, "general", "session", None)}})
    monkeypatch.setattr(server, "_session_tool_progress_mode", lambda _: "off")
    assert server._tool_progress_enabled("session")
    assert not server._tool_progress_enabled("ordinary-session")
