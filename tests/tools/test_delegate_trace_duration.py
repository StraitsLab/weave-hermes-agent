"""Child traces preserve only executor-measured, valid durations."""
import json

import pytest

from tools.delegate_tool_child_run import _build_tool_trace


@pytest.mark.parametrize("duration", [900, 0, -1, True, "900", 1.5, None])
def test_child_trace_duration_from_executor(duration):
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "a", "function": {
            "name": "read_file", "arguments": json.dumps({"path": "a.py"})}}]},
        {"role": "tool", "tool_call_id": "a", "content": "ok",
         "display_metadata": {"duration_ms": duration}},
    ]
    trace = _build_tool_trace(messages)[0]
    if type(duration) is int and duration >= 0:
        assert trace["duration_ms"] == duration
    else:
        assert "duration_ms" not in trace
