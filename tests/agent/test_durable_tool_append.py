"""P0: steer / run-budget wrap-up text appended to an already-saved tool row
must be durable in state.db and survive tool-output pruning and compaction.

A tool row is flushed to state.db right after it is appended; every later
flush skips rows carrying the persisted marker. Text appended to that row
afterwards (post-batch /steer drain, pre-API /steer drain, run-budget
wrap-up notice) therefore never reached the store, and every path that
reloads history from state.db (gateway native submit, resume) replayed the
tool row WITHOUT the user's steer — the model forgot it and the provider
prompt cache missed from that row onward.

T0a  real AIAgent + real SessionDB, mock provider: reload == live send.
T0b  pruning / pressure / lean demotion / both compaction serializers keep
     the steer text of a genuinely large prunable tool body.
T0c  text+image tool results: image block keeps its type after persist,
     reload and branch copy; the steer is one trailing text block.
T0d  a tool row with no ``_row_id`` at append time: the flush carries it.
T0e  guarded update refused (stored content changed underneath): no crash,
     the steer is re-queued instead of silently diverging.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.prompt_builder import STEER_MARKER_OPEN, format_steer_marker
from hermes_state import SessionDB

IMG_URL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"


# ---------------------------------------------------------------------------
# Mock provider (same shape as tests/agent/test_api_content_sidecar.py)
# ---------------------------------------------------------------------------


def _tc_resp(name: str, args: str, call_id: str = "call_1") -> dict:
    return {
        "id": "m",
        "choices": [{"index": 0, "message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": name, "arguments": args}}]},
            "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
    }


def _text_resp(text: str) -> dict:
    return {
        "id": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
    }


class _Handler(BaseHTTPRequestHandler):
    captured: list = []
    queue: list = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length).decode())
        type(self).captured.append(req)
        # Only chat requests consume the scripted queue; the model-metadata
        # probe also POSTs here.
        if "messages" in req and type(self).queue:
            resp = type(self).queue.pop(0)
        else:
            resp = _text_resp("DONE")
        msg = resp["choices"][0]["message"]
        if req.get("stream") is True:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [{"id": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}]
            if msg.get("content"):
                chunks.append({"id": "m", "choices": [{"index": 0, "delta": {"content": msg["content"]}, "finish_reason": None}]})
            for ti, tc in enumerate(msg.get("tool_calls") or []):
                chunks.append({"id": "m", "choices": [{"index": 0, "delta": {"tool_calls": [{
                    "index": ti, "id": tc["id"], "type": "function",
                    "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}}]},
                    "finish_reason": None}]})
            chunks.append({"id": "m", "choices": [{"index": 0, "delta": {},
                           "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}]})
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *a, **kw):
        pass


@pytest.fixture()
def wire():
    """Mock provider + throwaway HERMES_HOME (mktemp) + one shared SessionDB."""
    _Handler.captured = []
    _Handler.queue = []
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    home = tempfile.mkdtemp(prefix="hermes_p0_append_")
    os.makedirs(os.path.join(home, ".hermes"))
    prev = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = os.path.join(home, ".hermes")
    db = SessionDB(db_path=Path(home) / "state.db")
    sid = "sess-p0"

    from run_agent import AIAgent

    def make_agent():
        agent = AIAgent(
            api_key="test-key", base_url=f"http://127.0.0.1:{port}/v1",
            provider="openai-compat", model="test-model",
            max_iterations=10, enabled_toolsets=[],
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            save_trajectories=False, platform="cli",
            session_db=db, session_id=sid,
        )
        agent.valid_tool_names = {"read_file"}
        return agent

    try:
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            yield make_agent, _Handler, db, sid
    finally:
        srv.shutdown()
        db.close()
        shutil.rmtree(home, ignore_errors=True)
        if prev is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = prev


def _chat(handler) -> list:
    return [r for r in handler.captured if "messages" in r]


def _tool_msgs(req: dict) -> list:
    return [m for m in req["messages"] if m.get("role") == "tool"]


def _reloaded_tool_rows(db, sid, **kw) -> list:
    return [m for m in db.get_messages_as_conversation(sid, **kw) if m.get("role") == "tool"]


READ_ARGS = '{"path": "/nonexistent-p0-durable-append"}'


# ---------------------------------------------------------------------------
# T0a — end to end: reload replays exactly what was sent live
# ---------------------------------------------------------------------------


class TestT0aDurableAppendEndToEnd:
    def test_post_batch_steer_is_durable_and_replayed_byte_identical(self, wire):
        make_agent, handler, db, sid = wire
        handler.queue += [_tc_resp("read_file", READ_ARGS), _text_resp("done")]
        agent = make_agent()
        # The steer lands while the tool runs: after the tool row was appended
        # AND flushed, before the post-batch drain.
        agent.tool_complete_callback = lambda *a, **k: agent.steer("POST-BATCH STEER: check auth.log too")

        agent.run_conversation("look around", conversation_history=[], task_id="t1")

        reqs = _chat(handler)
        assert len(reqs) == 2
        live_tool = _tool_msgs(reqs[1])[-1]
        assert "POST-BATCH STEER: check auth.log too" in live_tool["content"]
        assert STEER_MARKER_OPEN in live_tool["content"]

        stored = _reloaded_tool_rows(db, sid)[-1]
        assert stored["content"] == live_tool["content"]  # byte-identical

        # Gateway native-submit reload (gateway/session.py load_transcript):
        # turn N+1 must replay the tool row exactly as turn N sent it.
        history = db.get_messages_as_conversation(sid, repair_alternation=True)
        handler.captured = []
        make_agent().run_conversation("next", conversation_history=history, task_id="t2")
        replayed = _tool_msgs(_chat(handler)[0])[-1]
        assert json.dumps(replayed, sort_keys=True) == json.dumps(live_tool, sort_keys=True)

    def test_pre_api_steer_is_durable(self, wire):
        make_agent, handler, db, sid = wire
        handler.queue += [_tc_resp("read_file", READ_ARGS), _text_resp("done")]
        agent = make_agent()

        # step_callback fires at the top of iteration 2, before the pre-API
        # drain — i.e. the steer "arrived during the API call".
        def _step(n, _prev):
            if n == 2:
                agent.steer("PRE-API STEER: focus on errors")

        agent.step_callback = _step
        agent.run_conversation("look around", conversation_history=[], task_id="t1")

        live_tool = _tool_msgs(_chat(handler)[1])[-1]
        assert "PRE-API STEER: focus on errors" in live_tool["content"]
        stored = _reloaded_tool_rows(db, sid)[-1]
        assert stored["content"] == live_tool["content"]

    def test_run_budget_wrapup_notice_is_durable(self, wire):
        from agent.conversation_loop import RUN_BUDGET_WRAPUP_NOTICE

        make_agent, handler, db, sid = wire
        handler.queue += [_tc_resp("read_file", READ_ARGS), _text_resp("done")]
        agent = make_agent()
        agent.run_budget_seconds = 100.0

        def _step(n, _prev):
            if n == 2:  # push the turn past 80% of its wall-clock budget
                agent._run_budget_started_at = time.time() - 95.0

        agent.step_callback = _step
        agent.run_conversation("look around", conversation_history=[], task_id="t1")

        live_tool = _tool_msgs(_chat(handler)[1])[-1]
        assert RUN_BUDGET_WRAPUP_NOTICE in live_tool["content"]
        stored = _reloaded_tool_rows(db, sid)[-1]
        assert stored["content"] == live_tool["content"]


# ---------------------------------------------------------------------------
# Helpers for unit-level tests on a real AIAgent + real SessionDB
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path):
    from run_agent import AIAgent

    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "sess-unit"
    db.create_session(session_id=sid, source="cli")
    agent = AIAgent(
        api_key="test-key", base_url="https://openrouter.ai/api/v1",
        quiet_mode=True, skip_context_files=True, skip_memory=True,
        session_db=db, session_id=sid,
    )
    agent._session_db_created = True
    try:
        yield agent, db, sid
    finally:
        db.close()


def _turn(tool_content, call_id="call_1"):
    return [
        {"role": "user", "content": "take a screenshot"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": call_id, "type": "function",
             "function": {"name": "vision_analyze", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": call_id, "content": tool_content},
    ]


def _image_blocks():
    return [
        {"type": "text", "text": "Image attached natively"},
        {"type": "image_url", "image_url": {"url": IMG_URL}},
    ]


def _assert_image_then_trailing_text(content, trailing_text):
    assert isinstance(content, list), f"list content was flattened: {content!r}"
    assert len(content) == 3
    assert content[0] == {"type": "text", "text": "Image attached natively"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"] == IMG_URL
    assert content[2] == {"type": "text", "text": trailing_text}


# ---------------------------------------------------------------------------
# T0c — text + image tool results
# ---------------------------------------------------------------------------


class TestT0cMultimodalToolRow:
    def test_post_batch_steer_on_image_row_survives_reload_and_branch(self, store):
        agent, db, sid = store
        messages = _turn(_image_blocks())
        agent._flush_messages_to_session_db(messages)
        assert isinstance(messages[-1].get("_row_id"), int)

        agent.steer("look at the red button")
        agent._apply_pending_steer_to_tool_results(messages, 1)
        marker = format_steer_marker("look at the red button").lstrip()
        _assert_image_then_trailing_text(messages[-1]["content"], marker)

        stored = _reloaded_tool_rows(db, sid)[-1]
        _assert_image_then_trailing_text(stored["content"], marker)
        assert stored["content"] == messages[-1]["content"]

        # Branch copy (native fork) keeps the same row.
        assert db.safe_fork_session(sid, "sess-unit-branch") == "created"
        branched = _reloaded_tool_rows(db, "sess-unit-branch")[-1]
        _assert_image_then_trailing_text(branched["content"], marker)

    def test_pre_api_steer_on_image_row_is_durable(self, store):
        from agent.conversation_loop import drain_pending_steer_before_api_call

        agent, db, sid = store
        messages = _turn(_image_blocks())
        agent._flush_messages_to_session_db(messages)
        agent.steer("zoom in")
        assert drain_pending_steer_before_api_call(agent, messages) is True
        marker = format_steer_marker("zoom in").lstrip()
        _assert_image_then_trailing_text(messages[-1]["content"], marker)
        stored = _reloaded_tool_rows(db, sid)[-1]
        assert stored["content"] == messages[-1]["content"]

    def test_wrapup_on_image_row_is_durable(self, store):
        from agent.conversation_loop import (
            RUN_BUDGET_WRAPUP_NOTICE,
            _maybe_inject_run_budget_wrapup,
        )

        agent, db, sid = store
        messages = _turn(_image_blocks())
        agent._flush_messages_to_session_db(messages)
        agent.run_budget_seconds = 100.0
        agent._run_budget_started_at = time.time() - 95.0
        agent._run_budget_wrapup_injected = False
        assert _maybe_inject_run_budget_wrapup(agent, messages) is True
        _assert_image_then_trailing_text(messages[-1]["content"], RUN_BUDGET_WRAPUP_NOTICE)
        stored = _reloaded_tool_rows(db, sid)[-1]
        assert stored["content"] == messages[-1]["content"]


# ---------------------------------------------------------------------------
# T0d — row not yet saved when the steer lands: the flush carries it
# ---------------------------------------------------------------------------


class TestT0dUnsavedRowFlushCarriesAppend:
    def test_string_row_without_row_id(self, store):
        agent, db, sid = store
        messages = _turn("plain output")
        assert "_row_id" not in messages[-1]
        agent.steer("late note")
        agent._apply_pending_steer_to_tool_results(messages, 1)
        agent._flush_messages_to_session_db(messages)
        stored = _reloaded_tool_rows(db, sid)[-1]
        assert stored["content"] == "plain output" + format_steer_marker("late note")

    def test_list_row_without_row_id_keeps_image_type(self, store):
        agent, db, sid = store
        messages = _turn(_image_blocks())
        agent.steer("late note")
        agent._apply_pending_steer_to_tool_results(messages, 1)
        agent._flush_messages_to_session_db(messages)
        stored = _reloaded_tool_rows(db, sid)[-1]
        _assert_image_then_trailing_text(
            stored["content"], format_steer_marker("late note").lstrip()
        )


# ---------------------------------------------------------------------------
# T0e — guarded update refused: no crash, steer re-queued
# ---------------------------------------------------------------------------


class TestT0eGuardedRefusal:
    def test_stored_content_changed_underneath(self, store):
        agent, db, sid = store
        messages = _turn("original output")
        agent._flush_messages_to_session_db(messages)
        row_id = messages[-1]["_row_id"]

        def _rewrite(conn):
            conn.execute("UPDATE messages SET content = ? WHERE id = ?",
                         ("rewritten by someone else", row_id))

        db._execute_write(_rewrite)

        agent.steer("keep this")
        agent._apply_pending_steer_to_tool_results(messages, 1)  # must not raise

        # Not delivered through a row that cannot be made durable ...
        assert messages[-1]["content"] == "original output"
        # ... and not lost: re-queued for the next delivery point / next turn.
        assert agent._pending_steer == "keep this"
        stored = _reloaded_tool_rows(db, sid)[-1]
        assert stored["content"] == "rewritten by someone else"


# ---------------------------------------------------------------------------
# T0b — pruning and compaction keep the steer
# ---------------------------------------------------------------------------


BIG_BODY = "\n".join(f"line {i}: " + "x" * 80 for i in range(400))  # ~36K chars
LONG_STEER = "LONG STEER START " + ("please keep the migration order intact; " * 50) + "LONG STEER END"


def _prunable_history(steer: str = "STEER-KEEP-ME use the staging DB"):
    tool = BIG_BODY + format_steer_marker(steer)
    msgs = [{"role": "user", "content": "run the tests"}]
    msgs.append({"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_big", "type": "function",
         "function": {"name": "terminal", "arguments": '{"command": "pytest -q"}'}}]})
    msgs.append({"role": "tool", "tool_call_id": "call_big", "content": tool})
    for i in range(6):
        msgs.append({"role": "assistant", "content": f"step {i}"})
        msgs.append({"role": "user", "content": f"ok {i}"})
    return msgs


def _compressor():
    from agent.context_compressor import ContextCompressor

    return ContextCompressor(model="test/model", quiet_mode=True)


class TestT0bPruneAndCompactionKeepSteer:
    def test_prune_old_tool_results_keeps_steer(self):
        msgs = _prunable_history()
        out, pruned = _compressor()._prune_old_tool_results(msgs, protect_tail_count=4)
        new = out[2]["content"]
        assert pruned >= 1 and len(new) < 2000, "prune did not fire"
        assert new.startswith("[terminal]")
        assert new.endswith(format_steer_marker("STEER-KEEP-ME use the staging DB"))

    def test_second_prune_does_not_resummarize_summary_plus_steer(self):
        msgs = _prunable_history(LONG_STEER)
        c = _compressor()
        once, _ = c._prune_old_tool_results(msgs, protect_tail_count=4)
        twice, _ = c._prune_old_tool_results(once, protect_tail_count=4)
        assert twice[2]["content"] == once[2]["content"]
        assert LONG_STEER in twice[2]["content"]

    def test_pressure_demotion_keeps_steer(self):
        msgs = _prunable_history()
        # Everything protected by count; pressure pass must still demote.
        out, pruned = _compressor()._prune_old_tool_results(
            msgs, protect_tail_count=len(msgs), protect_tail_tokens=500,
        )
        new = out[2]["content"]
        assert pruned >= 1 and len(new) < 2000, "pressure demotion did not fire"
        assert "STEER-KEEP-ME use the staging DB" in new

    def test_lean_tail_demotion_keeps_steer(self):
        msgs = _prunable_history()
        c = _compressor()
        c._session_id = "sess-x"
        out = c._demote_stale_tail_tools(
            msgs + [
                m
                for k in range(7)
                for m in (
                    {"role": "assistant", "content": "", "tool_calls": [
                        {"id": f"c{k}", "type": "function",
                         "function": {"name": "terminal", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": f"c{k}", "content": "short"},
                    {"role": "user", "content": f"u{k}"},
                )
            ],
            tail_start=0,
        )
        new = out[2]["content"]
        assert "output demoted at compaction" in new, "lean demotion did not fire"
        assert "STEER-KEEP-ME use the staging DB" in new

    def test_llm_serializer_keeps_long_steer_past_truncation(self):
        msgs = _prunable_history(LONG_STEER)
        c = _compressor()
        text = c._serialize_for_summary(msgs)
        assert "...[truncated]..." in text, "per-message truncation did not fire"
        assert LONG_STEER in text

    def test_static_fallback_keeps_steer(self):
        msgs = _prunable_history()
        summary = _compressor()._build_static_fallback_summary(msgs, reason="test")
        assert "STEER-KEEP-ME use the staging DB" in summary
