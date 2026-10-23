"""WEV-3349: the cell writer streams a reply into Supabase chat_items and saves it as the same row."""
import itertools
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from gateway import weave_chat_sync as sync


@pytest.fixture
def server(monkeypatch):
    """A stand-in for the Supabase Data API: records chat_cell_write calls and answers like the SQL verb."""
    calls, rows = [], {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append({"path": self.path, "apikey": self.headers.get("apikey"), **body})
            out = []
            for item in body["p_items"]:
                cell_key = item.get("cell_key")
                item_id = item.get("item_id")
                row = rows.get(item_id) if item_id else None
                if row is None and cell_key:
                    row = next((r for r in rows.values() if r.get("cell_key") == cell_key), None)
                if row is None:
                    pk = item_id or cell_key
                    row = rows.setdefault(pk, {"rev": 0, "text": "", "seq": len(rows), "created_at": item.get("at") or f"9999-{len(rows):04d}", **item})
                    row["text"] = item.get("append", (item.get("body") or {}).get("text", ""))
                else:
                    if "append" in item and item.get("rev") != row["rev"]:
                        out.append({"item_id": item.get("item_id"), "rev": row["rev"], "conflict": True})
                        continue
                    row["rev"] += 1
                    row["text"] = row["text"] + item["append"] if "append" in item else (item.get("body") or {}).get("text", row["text"])
                    row.update({k: v for k, v in item.items() if k in ("state", "cell_key")})
                row.update({k: v for k, v in (item.get("body") or {}).items() if k == "outcome"})
                out.append({"item_id": item.get("item_id"), "rev": row["rev"]})
            data = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setattr(sync, "_WRITER", None)
    monkeypatch.setattr(sync, "FLUSH_S", 0.05)
    yield f"http://127.0.0.1:{httpd.server_port}", calls, rows
    httpd.shutdown()


def secrets(url):
    values = {"WEAVE_CHAT_SUPABASE_URL": url.replace("http://", "https://"), "WEAVE_CHAT_SUPABASE_KEY": "sb_publishable_x",
              "WEAVE_API_MCP_BEARER": "cell-bearer"}
    return lambda name: values.get(name, "")


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


def run_turn(url, session, ref, events, saved, monkeypatch, started=True):
    # The fake server is plain http; the writer only accepts https settings, so swap the scheme back at call time.
    real = sync._call
    monkeypatch.setattr(sync, "_call", lambda live, items: real({**live, "url": live["url"].replace("https://", "http://")}, items))
    for event in ([{"type": "turn.started"}] if started else []) + events:
        sync.publish(session, ref, event, lambda: saved, secrets(url))


def test_reply_streams_into_one_row_and_is_saved_as_that_row(server, monkeypatch):
    url, calls, rows = server
    now = time.time()
    saved = [
        {"id": 5, "role": "assistant", "content": "an older answer", "timestamp": now - 500},
        {"id": 7, "role": "user", "content": "hello", "timestamp": now + 0.5, "platform_message_id": "ref1"},
        {"id": 8, "role": "assistant", "content": "", "tool_calls": [{"id": "t"}], "timestamp": now + 1.0},
        {"id": 9, "role": "tool", "content": "secret tool output", "timestamp": now + 1.5},
        {"id": 10, "role": "assistant", "content": "Hi there", "timestamp": now + 2.0},
        {"id": 11, "role": "user", "content": "[System: Continue now.]", "timestamp": now + 3.0},
    ]
    run_turn(url, "weave-abc", "ref1", [
        {"type": "tool.started", "tool_name": "web_search"},
        {"type": "tool.completed", "tool_name": "web_search"},
        {"type": "assistant.delta", "delta": "Hi"},
        {"type": "assistant.delta", "delta": " there"},
        {"type": "turn.completed"},
    ], saved, monkeypatch)
    answer_key = f"assistant:{now + 2.0!r}"
    wait_for(lambda: any(r.get("cell_key") == answer_key for r in rows.values()))
    live_id = sync.uuid.uuid5(sync.uuid.NAMESPACE_URL, "weave-chat:ref1").__str__()
    assert rows[live_id]["text"] == "Hi there" and rows[live_id]["state"] == "final"
    assert rows[live_id]["cell_key"] == answer_key  # the streamed row IS the saved answer
    sent = [i for c in calls for i in c["p_items"]]
    assert all(c["p_bearer"] == "cell-bearer" and c["p_session_ref"] == "weave-abc" and c["apikey"] == "sb_publishable_x"
               and c["path"] == "/rest/v1/rpc/chat_cell_write" for c in calls)
    assert "secret tool output" not in json.dumps(calls)  # the tool channel never leaves the cell
    assert "[System:" not in json.dumps(calls)
    finals = [i.get("cell_key") for i in sent if i.get("state") == "final"]
    assert set(finals) == {f"user:{now + 0.5!r}", answer_key}  # an older answer is never re-sent
    assert finals[-1] == answer_key  # the turn's answer lands last: the apps settle on it


def test_text_between_tools_streams_into_its_own_row_in_order(server, monkeypatch):
    """Mid-turn updates and the answer are separate rows, each created when its text starts (2026-10-17 RedMart turn).

    One live row for the whole turn was created first, then took the answer at the end, so the apps (which
    order by created_at) showed the answer above the updates it came after.
    """
    url, calls, rows = server
    now = time.time()
    saved = [
        {"id": 1, "role": "user", "content": "Add it", "timestamp": now + 0.1, "platform_message_id": "ref-seg"},
        {"id": 2, "role": "assistant", "content": "Signed in. Searching.", "tool_calls": [{"id": "a"}], "timestamp": now + 1.0},
        {"id": 3, "role": "tool", "content": "page", "timestamp": now + 1.5},
        {"id": 4, "role": "assistant", "content": "Found it. Adding.", "tool_calls": [{"id": "b"}], "timestamp": now + 2.0},
        {"id": 5, "role": "tool", "content": "added?", "timestamp": now + 2.5},
        {"id": 6, "role": "assistant", "content": "I can't confirm it was added.", "timestamp": now + 3.0},
    ]
    def step(*events):  # one flush per step, as when the model pauses for a tool
        sent = len(calls)
        run_turn(url, "weave-abc", "ref-seg", list(events), saved, monkeypatch, started=False)
        wait_for(lambda: len(calls) > sent)

    step({"type": "assistant.delta", "delta": "Signed in. Searching."})
    step({"type": "tool.started", "tool_name": "browse"})
    step({"type": "tool.completed", "tool_name": "browse"}, {"type": "assistant.delta", "delta": "Found it. Adding."})
    step({"type": "tool.started", "tool_name": "browse"})
    step({"type": "tool.completed", "tool_name": "browse"}, {"type": "assistant.delta", "delta": "I can't confirm it was added."})
    step({"type": "turn.completed"})
    live_id = str(sync.uuid.uuid5(sync.uuid.NAMESPACE_URL, "weave-chat:ref-seg"))
    wait_for(lambda: sum(r.get("state") == "final" and r.get("role") == "assistant" for r in rows.values()) >= 3)
    shown = [r["text"] for r in sorted(rows.values(), key=lambda r: r["seq"])
             if r.get("role") == "assistant" and r.get("state") == "final"]
    assert shown == ["Signed in. Searching.", "Found it. Adding.", "I can't confirm it was added."]
    assert rows[live_id]["text"] == "Signed in. Searching."  # the first row keeps only its own text
    assert [r["cell_key"] for r in sorted(rows.values(), key=lambda r: r["seq"]) if r.get("role") == "assistant"] == [
        f"assistant:{now + 1.0!r}", f"assistant:{now + 2.0!r}", f"assistant:{now + 3.0!r}"]  # each adopts its saved row


def test_a_closed_row_without_a_saved_message_is_hidden(server, monkeypatch):
    """Streamed text before a tool that Hermes did not save on its own never shows twice next to the answer."""
    url, calls, rows = server
    now = time.time()
    saved = [{"id": 1, "role": "user", "content": "hi", "timestamp": now + 0.1, "platform_message_id": "ref-gap"},
             {"id": 2, "role": "assistant", "content": "", "tool_calls": [{"id": "a"}], "timestamp": now + 1.0},
             {"id": 3, "role": "assistant", "content": "Hi there", "timestamp": now + 2.0}]
    for events in ([{"type": "assistant.delta", "delta": "Hi"}, {"type": "tool.started", "tool_name": "x"}],
                   [{"type": "tool.completed", "tool_name": "x"}, {"type": "assistant.delta", "delta": " there"}],
                   [{"type": "turn.completed"}]):
        sent = len(calls)
        run_turn(url, "weave-abc", "ref-gap", events, saved, monkeypatch, started=False)
        wait_for(lambda: len(calls) > sent)
    live_id = str(sync.uuid.uuid5(sync.uuid.NAMESPACE_URL, "weave-chat:ref-gap"))
    wait_for(lambda: rows.get(live_id, {}).get("state") == "removed")
    shown = [r["text"] for r in rows.values() if r.get("role") == "assistant" and r.get("state") == "final"]
    assert shown == ["Hi there"]


def test_cards_and_their_resolution_are_rows(server, monkeypatch):
    url, calls, rows = server
    run_turn(url, "weave-abc", "ref2", [
        {"type": "approval.request", "request_id": "r1", "pattern_key": "plugin_rule:018f", "command": "rm -rf /",
         "description": "Delete files", "choices": ["once", "session", "always", "deny"]},
        {"type": "clarify.request", "clarify_id": "c1", "question": "Which?", "choices": ["a", "b"], "multi_select": False},
        {"type": "turn.completed"},
    ], [], monkeypatch)
    wait_for(lambda: rows.get("approval:r1", {}).get("state") == "resolved")
    first = next(i for c in calls for i in c["p_items"] if i.get("cell_key") == "approval:r1")
    assert first["state"] == "pending" and first["body"]["choices"] == ["once", "deny"]
    assert first["body"]["approval_ref"] == "018f" and "command" not in first["body"]
    assert first["body"]["native_request_ref"] == "ref2"  # WEV-3404: an answer row finds the turn by this
    assert rows["clarify:c1"]["state"] == "resolved"


def test_a_tool_card_lands_on_the_answer_row(server, monkeypatch):
    """WEV-3395: the last card a hub tool returned rides the answer row as body.output, the field the apps draw."""
    url, calls, rows = server
    now = time.time()
    card = {"kind": "output_blocks", "major": 1, "header": {"title": "SQ 638"}, "fallback_text": "SQ 638 on time"}
    saved = [{"id": 1, "role": "user", "content": "SQ 638?", "timestamp": now + 0.5, "platform_message_id": "ref-card"},
             {"id": 2, "role": "assistant", "content": "On time.", "timestamp": now + 2.0}]
    run_turn(url, "weave-abc", "ref-card", [
        {"type": "tool.completed", "tool_name": "mcp__weave_connectors__show_flight_status", "card": card},
        {"type": "assistant.delta", "delta": "On time."},
        {"type": "turn.completed"},
    ], saved, monkeypatch)
    answer_key = f"assistant:{now + 2.0!r}"
    wait_for(lambda: any(r.get("cell_key") == answer_key for r in rows.values()))
    answer = next(i for c in calls for i in c["p_items"] if i.get("cell_key") == answer_key)
    assert answer["body"]["output"] == card and answer["body"]["text"] == "On time."
    user = next(i for c in calls for i in c["p_items"] if i.get("cell_key") == f"user:{now + 0.5!r}")
    assert "output" not in user["body"]


def test_a_card_reaches_the_live_row_before_the_turn_ends(server, monkeypatch):
    """WEV-3464: a card goes on the writing row when its tool completes, in a write of its own (no text append)."""
    url, calls, rows = server
    card = {"kind": "output_blocks", "major": 1, "header": {"title": "Weather"}, "fallback_text": "31 C"}
    run_turn(url, "weave-abc", "ref-early", [
        {"type": "assistant.delta", "delta": "Checking."},
        {"type": "tool.completed", "tool_name": "mcp__weave_connectors__weather_now", "card": card},
    ], [], monkeypatch)
    live_id = str(sync.uuid.uuid5(sync.uuid.NAMESPACE_URL, "weave-chat:ref-early"))
    wait_for(lambda: any(i.get("item_id") == live_id and (i.get("body") or {}).get("output") == card
                         for c in calls for i in c["p_items"]))
    write = next(i for c in calls for i in c["p_items"] if (i.get("body") or {}).get("output") == card)
    assert write["state"] == "writing" and "append" not in write  # body-only write: the push carries the card
    assert rows[live_id]["text"] == "Checking."  # the streamed text is untouched


def test_a_card_tool_starting_shows_its_card_name(server, monkeypatch):
    """WEV-3464: the model starting show_<card> sets the row status to that tool at once (the apps draw a placeholder)."""
    url, calls, _rows = server
    run_turn(url, "weave-abc", "ref-gen", [
        {"type": "tool.generating", "tool_name": "mcp__weave_connectors__show_compare_products"},
    ], [], monkeypatch)
    wait_for(lambda: any((i.get("body") or {}).get("status") == "mcp__weave_connectors__show_compare_products"
                         for c in calls for i in c["p_items"]))


def test_off_without_settings_and_never_raises(server, monkeypatch):
    url, calls, _rows = server
    sync.publish("weave-abc", "ref3", {"type": "turn.started"}, list, lambda name: "")
    sync.publish("weave-abc", "ref3", {"type": "assistant.delta", "delta": "x"}, list, lambda name: "")
    sync.publish("not-weave", "ref4", {"type": "turn.started"}, list, secrets(url))
    # Nothing was enqueued: the writer never started, or its queue is empty and idle.
    assert sync._WRITER is None or sync._WRITER.events.empty()
    assert calls == []


def test_a_failed_turn_hides_its_partial_reply(server, monkeypatch):
    url, _calls, rows = server
    old = [{"id": 1, "role": "assistant", "content": "yesterday's answer", "timestamp": time.time() - 3600}]
    run_turn(url, "weave-abc", "ref5", [{"type": "assistant.delta", "delta": "half"}, {"type": "turn.failed"}], old, monkeypatch)
    live_id = str(sync.uuid.uuid5(sync.uuid.NAMESPACE_URL, "weave-chat:ref5"))
    wait_for(lambda: rows.get(live_id, {}).get("state") == "removed")
    assert rows[live_id]["outcome"] == "failed"  # an older answer is never adopted as this turn's


def test_a_stopped_turn_keeps_what_it_wrote(server, monkeypatch):
    url, _calls, rows = server
    run_turn(url, "weave-abc", "ref5s", [{"type": "assistant.delta", "delta": "The port began"},
                                          {"type": "turn.failed", "stopped": True}], [], monkeypatch)
    live_id = str(sync.uuid.uuid5(sync.uuid.NAMESPACE_URL, "weave-chat:ref5s"))
    wait_for(lambda: rows.get(live_id, {}).get("state") == "final")
    assert rows[live_id]["text"] == "The port began" and rows[live_id]["outcome"] == "stopped"


def test_a_stopped_turn_with_nothing_written_ends_hidden(server, monkeypatch):
    url, _calls, rows = server
    run_turn(url, "weave-abc", "ref5e", [{"type": "turn.failed", "stopped": True}], [], monkeypatch)
    live_id = str(sync.uuid.uuid5(sync.uuid.NAMESPACE_URL, "weave-chat:ref5e"))
    wait_for(lambda: rows.get(live_id, {}).get("state") == "removed")
    assert rows[live_id]["outcome"] == "stopped"


def test_a_turn_without_streamed_text_still_ends_on_its_live_row(server, monkeypatch):
    url, _calls, rows = server
    now = time.time()
    saved = [{"id": 1, "role": "user", "content": "hi", "timestamp": now, "platform_message_id": "ref6"}, {"id": 2, "role": "assistant", "content": "Done.", "timestamp": now + 1}]
    run_turn(url, "weave-abc", "ref6", [{"type": "turn.completed"}], saved, monkeypatch)
    live_id = str(sync.uuid.uuid5(sync.uuid.NAMESPACE_URL, "weave-chat:ref6"))
    wait_for(lambda: rows.get(live_id, {}).get("state") == "final")
    assert rows[live_id]["text"] == "Done."


def test_an_immediate_turn_without_turn_started_still_streams(server, monkeypatch):
    # The gateway emits turn.started only for queued or steered submits; an immediate turn begins with its text.
    url, _calls, rows = server
    now = time.time()
    saved = [{"id": 1, "role": "user", "content": "hi", "timestamp": now, "platform_message_id": "ref7"}, {"id": 2, "role": "assistant", "content": "Hello", "timestamp": now + 1}]
    run_turn(url, "weave-abc", "ref7", [{"type": "assistant.delta", "delta": "Hel"}, {"type": "assistant.delta", "delta": "lo"},
                                         {"type": "turn.completed"}], saved, monkeypatch, started=False)
    live_id = str(sync.uuid.uuid5(sync.uuid.NAMESPACE_URL, "weave-chat:ref7"))
    wait_for(lambda: rows.get(live_id, {}).get("state") == "final")
    assert rows[live_id]["text"] == "Hello"
    # A late event for a finished turn reopens nothing.
    sync.publish("weave-abc", "ref7", {"type": "assistant.delta", "delta": "x"}, lambda: saved, secrets(url))
    wait_for(lambda: sync._WRITER.events.empty())
    assert "ref7" not in sync._WRITER.live


def test_terminal_callback_waits_for_final_write(server, monkeypatch):
    url, _calls, _rows = server
    entered, proceed, completed = threading.Event(), threading.Event(), threading.Event()
    original = sync._Writer._finish
    def finish(self, live):
        entered.set()
        assert proceed.wait(3)
        original(self, live)
    monkeypatch.setattr(sync._Writer, "_finish", finish)
    real = sync._call
    monkeypatch.setattr(sync, "_call", lambda live, items: real({**live, "url": live["url"].replace("https://", "http://")}, items))
    try:
        assert sync.publish("weave-callback", "callback-ref", {"type": "turn.completed"},
                            list, secrets(url), on_finished=completed.set)
        assert entered.wait(2)
        assert not completed.is_set()
        proceed.set()
        assert completed.wait(2)
    finally:
        proceed.set()


@pytest.mark.parametrize("failure", ["flush", "committed_flush", "finish", "committed_finish"])
def test_a_finished_turn_retries_its_write_without_delaying_completion(server, monkeypatch, failure):
    """WEV-3480: a timeout must not strand the writing row or hold the awake lease for retries."""
    from tests.waiting import wait_until

    url, _calls, rows = server
    ref = "retry-" + failure
    now = time.time()
    saved = [{"role": "user", "content": "hello", "timestamp": now, "platform_message_id": ref},
             {"role": "assistant", "content": "Hello", "timestamp": now + 1}]
    flushing = failure.endswith("flush")
    original = sync._Writer._flush if flushing else sync._Writer._finish
    attempts, callbacks = [], []
    entered, proceed = threading.Event(), threading.Event()

    def write(self, live):
        if live.get("ref") != ref:
            return  # earlier tests leave idle daemon writers alive
        attempts.append(live)
        if len(attempts) == 1:
            if failure.startswith("committed"):
                original(self, live)  # the server committed, but its response was lost
                if flushing:
                    live.update(sent=0, rev=None, shown=None)  # no acknowledgement reached the writer
            raise TimeoutError("TLS handshake timed out")
        entered.set()
        assert proceed.wait(5)
        original(self, live)

    monkeypatch.setattr(sync._Writer, "_flush" if flushing else "_finish", write)
    real = sync._call
    monkeypatch.setattr(sync, "_call", lambda live, items: real({**live, "url": live["url"].replace("https://", "http://")}, items))
    try:
        sync.publish("weave-retry", ref, {"type": "assistant.delta", "delta": "Hello"}, lambda: list(saved), secrets(url))
        sync.publish("weave-retry", ref, {"type": "turn.completed"}, lambda: list(saved), secrets(url),
                     on_finished=lambda: callbacks.append(ref))
        wait_until(lambda: callbacks, what="first attempt releases completion")
        assert ref in sync._WRITER.live and ref not in sync._ENDED
        # The next turn can run while this write retries. Its answer must not be adopted by the old turn.
        saved.append({"role": "assistant", "content": "Next answer", "timestamp": now + 2})
        sync.publish("weave-retry", ref, {"type": "assistant.delta", "delta": "late"}, lambda: list(saved), secrets(url))
        assert entered.wait(5)
        proceed.set()
        wait_until(lambda: ref not in sync._WRITER.live, what="final write retires the turn")
        item = str(sync.uuid.uuid5(sync.uuid.NAMESPACE_URL, "weave-chat:" + ref))
        assert rows[item]["state"] == "final" and rows[item]["text"] == "Hello"
        assert len(rows) == 2  # retries upsert the same user and assistant rows
        assert len(attempts) == 2
        assert list(sync._ENDED).count(ref) == 1
        assert callbacks == [ref]
    finally:
        proceed.set()


def test_final_write_backoff_is_capped_and_expires_without_holding_live_turns(monkeypatch, caplog):
    """The writer waits 1, 2, 4…30 seconds, gives up after ten minutes, and leaves active retries alone."""
    from collections import deque
    from types import SimpleNamespace

    clock, attempts, callbacks = [0.0], [], []
    monkeypatch.setattr(sync, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(sync, "_ENDED", deque())
    caplog.set_level("ERROR", logger=sync.__name__)
    writer = sync._Writer.__new__(sync._Writer)
    live = {"ended": True, "read": list, "on_finished": lambda: callbacks.append("done")}
    active = {}
    writer.live = {"ended": live, "active": active}
    monkeypatch.setattr(sync, "_WRITER", None)

    def fail(_self, turn):
        if turn is not live and turn is not active:
            return
        attempts.append(clock[0])
        raise TimeoutError("offline")

    monkeypatch.setattr(sync._Writer, "_flush", fail)
    writer._write("active", active)
    writer._write("active", active)
    assert attempts == [0, 0] and active == {} and writer.live["active"] is active
    attempts.clear()
    delays = []
    while "ended" in writer.live:
        writer._write("ended", live)
        if "ended" not in writer.live:
            break
        delays.append(live["retry_at"] - clock[0])
        count = len(attempts)
        clock[0] = live["retry_at"] - 0.01
        writer._write("ended", live)
        assert len(attempts) == count  # no busy retries between scheduled attempts
        clock[0] = min(live["retry_at"], sync.FINAL_RETRY_S)
    assert delays[:6] == [1, 2, 4, 8, 16, 30]
    assert all(delay == 30 for delay in delays[6:])
    assert clock[0] == 600 and attempts[-1] < 600
    assert list(sync._ENDED) == ["ended"] and callbacks == ["done"]
    assert "final write retries exhausted" in caplog.text


@pytest.fixture
def quiet_writer(monkeypatch):
    """Drive the real writer synchronously, recording each RPC batch."""
    import copy
    import queue

    writer = sync._Writer.__new__(sync._Writer)
    writer.live, writer.events, writer.last_at_us = {}, queue.Queue(), {}
    writer.floor_prune_at_us = 0
    monkeypatch.setattr(sync, "_ENDED", sync.collections.deque())
    monkeypatch.setattr(sync, "_WRITER", writer)
    calls = []

    def call(_live, items):
        calls.append(copy.deepcopy(items))
        return [{"item_id": item.get("item_id"), "rev": 1} for item in items]

    monkeypatch.setattr(sync, "_call", call)
    sync.publish("weave-regression", "writer-regression", {"type": "turn.started"}, list,
                 secrets("https://supabase.example"))
    return writer, writer.live["writer-regression"], calls


def test_same_clock_burst_has_strictly_increasing_formatted_timestamps(quiet_writer, monkeypatch):
    writer, live, calls = quiet_writer
    clock = 1760875200.123456
    monkeypatch.setattr(sync.time, "time", lambda: clock)
    for candidate in (clock, 1760875200.999999, clock - 100):
        stamps = [sync._iso(writer._next_at(live, candidate)) for _ in range(100)]
        assert all(a < b for a, b in itertools.pairwise(stamps))
    writer._take_out_rows(live, [{"role": "assistant", "kind": "text", "state": "final",
                                  "body": {"text": str(i)}} for i in range(6)])
    writer._flush(live)
    stamps = [row["at"] for row in calls[-1]]
    assert all(a < b for a, b in itertools.pairwise(stamps))


@pytest.mark.parametrize("queued_fallback", [False, True])
def test_passive_insert_burst_has_ordered_timestamps_without_active_turn(quiet_writer, queued_fallback):
    writer, live, calls = quiet_writer
    writer.live.clear()
    rows = [{"role": "assistant", "kind": "text", "state": "final", "body": {"text": str(i)}}
            for i in range(6)]
    if queued_fallback:
        writer._take(live["ref"], {"type": "out.rows", "rows": rows, "_conn": live})
    else:
        assert sync.publish_rows(live["session"], [], secrets("https://supabase.example"), raw_rows=rows)
    stamps = [row.get("at") for row in calls[-1]]
    assert all(stamps), stamps
    assert all(a < b for a, b in itertools.pairwise(stamps))


def test_saved_rows_without_source_timestamps_get_ordered_insert_times(quiet_writer):
    writer, live, calls = quiet_writer
    live["messages"] = [{"role": "user", "content": "Hi", "platform_message_id": live["ref"]},
                        {"role": "assistant", "content": "Hello"}]
    writer._take(live["ref"], {"type": "turn.completed"})
    writer._finish(live)
    stamps = [row.get("at") for row in calls[-1]]
    assert all(stamps), stamps
    assert all(a < b for a, b in itertools.pairwise(stamps))


def test_new_status_and_terminal_rows_never_rely_on_database_now(quiet_writer):
    writer, live, calls = quiet_writer
    writer._take(live["ref"], {"type": "tool.started", "tool_name": "browse"})
    writer._flush(live)
    status = calls[-1][0]
    assert status.get("at"), "a new status row must not use transaction now()"
    writer._take(live["ref"], {"type": "turn.failed"})
    writer._finish(live)
    terminal = calls[-1][-1]
    assert terminal.get("at") > status["at"], "a terminal row may be an insert without streamed text"


@pytest.mark.parametrize("with_text", [False, True])
@pytest.mark.parametrize("terminal", ["tool.completed", "tool.failed"])
def test_status_survives_tool_gaps_until_answer_replaces_it(quiet_writer, with_text, terminal):
    writer, live, calls = quiet_writer
    ref = live["ref"]
    if with_text:
        writer._take(ref, {"type": "assistant.delta", "delta": "Checking.", "message_uid": "checking"})
    writer._take(ref, {"type": "tool.started", "tool_name": "search"})
    writer._flush(live)
    count = len(calls)
    writer._take(ref, {"type": terminal, "tool_name": "search"})
    writer._flush(live)
    gap = [row for batch in calls[count:] for row in batch]
    assert not [row for row in gap if row.get("state") == "removed"
                or ("status" in row.get("body", {}) and not row["body"]["status"])], gap
    assert live["status"]
    writer._take(ref, {"type": "tool.generating", "tool_name": "browse"})
    writer._flush(live)
    writer._take(ref, {"type": terminal, "tool_name": "browse"})
    writer._flush(live)
    assert live["status"]
    count = len(calls)
    writer._take(ref, {"type": "assistant.delta", "delta": "Done.", "message_uid": "done"})
    writer._flush(live)
    for batch in calls[count:]:
        if any(row.get("state") == "removed" or row.get("body", {}).get("status", True) is None for row in batch):
            assert any(row.get("append") == "Done." for row in batch), batch
    live["messages"] = [{"role": "assistant", "content": "Done.", "message_uid": "done", "timestamp": time.time()}]
    writer._take(ref, {"type": "turn.completed"})
    writer._finish(live)
    assert calls[-1][-1]["body"]["status"] is None


@pytest.mark.parametrize("with_text", [False, True])
@pytest.mark.parametrize("terminal", ["tool.completed", "tool.failed"])
def test_writing_row_status_detail_follows_the_running_tool(quiet_writer, with_text, terminal):
    writer, live, calls = quiet_writer
    ref = live["ref"]
    if with_text:
        writer._take(ref, {"type": "assistant.delta", "delta": "Checking.", "message_uid": "checking"})
    for call_id, preview in (("c1", "git"), ("c2", "npm")):
        count = len(calls)
        writer._take(ref, {"type": "tool.started", "tool_call_id": call_id,
                           "tool_name": "terminal", "preview": preview})
        writer._flush(live)
        assert len(calls) > count, "same tool with a new preview must write again"
        row = calls[-1][-1]
        assert row["state"] == "writing"
        assert row["body"]["status"] == "terminal"
        assert row["body"]["status_detail"] == preview
        count = len(calls)
        writer._flush(live)
        assert len(calls) == count, "unchanged status/detail must not write again"

    writer._take(ref, {"type": terminal, "tool_call_id": "c2", "tool_name": "terminal"})
    writer._flush(live)
    assert calls[-1][-1]["body"]["status"] == "Thinking"
    assert calls[-1][-1]["body"]["status_detail"] is None
    writer._take(ref, {"type": "assistant.delta", "delta": "Done.", "message_uid": "done"})
    writer._flush(live)
    live["messages"] = [{"role": "assistant", "content": "Done.", "message_uid": "done", "timestamp": time.time()}]
    writer._take(ref, {"type": "turn.completed"})
    writer._finish(live)
    assert calls[-1][-1]["body"]["status_detail"] is None
    for batch in calls:
        for row in batch:
            if row.get("body", {}).get("status", False) is None:
                assert row["body"]["status_detail"] is None


@pytest.mark.parametrize("event", [
    {"type": "tool.generating", "tool_name": "terminal", "preview": "not-ready"},
    {"type": "tool.started", "tool_name": "terminal", "preview": {"command": "raw"}},
    {"type": "tool.started", "tool_name": "terminal"},
])
def test_writing_row_clears_detail_when_no_safe_preview_is_available(quiet_writer, event):
    writer, live, calls = quiet_writer
    writer._take(live["ref"], {"type": "tool.started", "tool_name": "terminal", "preview": "git"})
    writer._flush(live)
    writer._take(live["ref"], event)
    writer._flush(live)
    assert live["status_detail"] is None
    assert calls[-1][-1]["body"]["status_detail"] is None


def test_retracted_text_keeps_a_working_status_in_the_same_batch(quiet_writer):
    writer, live, calls = quiet_writer
    writer._take(live["ref"], {"type": "assistant.delta", "delta": "Partial", "message_uid": "partial"})
    writer._flush(live)
    writer._take(live["ref"], {"type": "assistant.retract", "message_uid": "partial"})
    writer._flush(live)
    assert any(row.get("state") == "writing" and row.get("body", {}).get("status") for row in calls[-1])


def test_status_and_saved_answer_share_the_last_batch_even_for_large_turns(quiet_writer):
    writer, live, calls = quiet_writer
    writer._take(live["ref"], {"type": "tool.started", "tool_name": "browse"})
    writer._flush(live)
    now = time.time()
    live["messages"] = [{"role": "assistant", "content": str(i), "timestamp": now + i} for i in range(100)]
    writer._take(live["ref"], {"type": "turn.completed"})
    writer._finish(live)
    removal = next(batch for batch in calls if any(row.get("state") == "removed" for row in batch))
    assert any(row.get("body", {}).get("text") == "99" for row in removal)
    assert all(len(batch) <= 100 for batch in calls)


def test_status_replacement_waits_for_saved_answer_when_no_text_streams(quiet_writer):
    writer, live, calls = quiet_writer
    writer._take(live["ref"], {"type": "tool.started", "tool_name": "browse"})
    writer._flush(live)
    writer._take(live["ref"], {"type": "tool.completed"})
    writer._flush(live)
    assert not any(row.get("state") == "removed" for batch in calls for row in batch)
    live["messages"] = [{"role": "assistant", "content": "Done.", "timestamp": time.time()}]
    writer._take(live["ref"], {"type": "turn.completed"})
    writer._flush(live)
    writer._finish(live)
    removed = [batch for batch in calls if any(row.get("state") == "removed" for row in batch)]
    assert len(removed) == 1
    assert removed[0][-1]["body"]["text"] == "Done."


@pytest.mark.parametrize("before_crash", ["status", "text", "no_rows"])
@pytest.mark.parametrize("restart_interrupted", [True, False])
def test_fresh_writer_retires_prior_status_only_for_restart_interruption(quiet_writer, monkeypatch, before_crash, restart_interrupted):
    writer, live, calls = quiet_writer
    ref, session = live["ref"], live["session"]
    status_id = str(sync.uuid.uuid5(sync.uuid.NAMESPACE_URL, f"weave-chat:{ref}:status"))
    text_id = str(sync.uuid.uuid5(sync.uuid.NAMESPACE_URL, f"weave-chat:{ref}"))
    rows = {}
    original_call = sync._call

    def store(conn, items):
        for item in items:
            rows[item["item_id"]] = item
        return original_call(conn, items)

    monkeypatch.setattr(sync, "_call", store)
    if before_crash == "status":
        writer._take(ref, {"type": "tool.started", "tool_name": "mcp__weave_connectors__web_search_exa"})
        writer._flush(live)
        assert rows[status_id]["state"] == "writing"
    elif before_crash == "text":
        writer._take(ref, {"type": "assistant.delta", "delta": "Partial reply"})
        writer._flush(live)
        assert rows[text_id]["state"] == "writing"
    # The database survives SIGKILL; the new process has no memory of open rows.
    writer.live.clear()
    calls.clear()
    assert sync.publish(session, ref, {"type": "turn.failed", "restart_interrupted": restart_interrupted},
                        list, secrets("https://supabase.example"))
    new_live = writer.live[ref]
    assert not new_live["status_open"]
    queued_ref, event = writer.events.get_nowait()
    writer._take(queued_ref, event)
    writer._write(ref, new_live)
    terminal = calls[-1]
    removed_status = [row for row in terminal if row.get("item_id") == status_id]
    if restart_interrupted:
        assert removed_status == [{"item_id": status_id, "role": "assistant", "kind": "text",
                                   "state": "removed", "body": {"status": None, "status_detail": None}}]
        assert terminal[-1]["item_id"] == text_id and terminal[-1]["state"] == "final"
        assert terminal[-1]["body"]["outcome"] == "failed"
        assert not any(row["state"] == "writing" for row in rows.values())
    else:
        assert not removed_status  # Ordinary terminal events keep their existing behavior.


def test_saved_only_replies_with_equal_source_times_keep_message_order(quiet_writer):
    writer, live, calls = quiet_writer
    now = time.time()
    live["messages"] = [{"role": "user", "content": "Hi", "timestamp": now,
                         "platform_message_id": live["ref"]},
                        {"role": "assistant", "content": "First", "timestamp": now + 1, "message_uid": "saved-first"},
                        {"role": "assistant", "content": "Second", "timestamp": now + 1, "message_uid": "saved-second"}]
    writer._take(live["ref"], {"type": "turn.completed"})
    writer._write(live["ref"], live)
    stamps = [row["at"] for row in calls[-1]]
    assert all(a < b for a, b in itertools.pairwise(stamps)), stamps


@pytest.mark.parametrize("existing_writer", [False, True])
def test_separate_passive_calls_share_conversation_timestamp_floor(quiet_writer, monkeypatch, existing_writer):
    writer, live, calls = quiet_writer
    writer.live.clear()
    if not existing_writer:
        monkeypatch.setattr(sync, "_WRITER", None)
    monkeypatch.setattr(sync.time, "time", lambda: 1760875200.123456)
    sec = secrets("https://supabase.example")
    for uid in ("passive-first", "passive-second"):
        assert sync.publish_rows(live["session"], [{"role": "assistant", "content": uid, "message_uid": uid}], sec)
    stamps = [row["at"] for batch in calls for row in batch]
    assert stamps[0] < stamps[1], stamps


def test_next_turn_shares_timestamp_floor_but_other_conversations_do_not(quiet_writer, monkeypatch):
    writer, live, calls = quiet_writer
    monkeypatch.setattr(sync.time, "time", lambda: 1760875200.123456)
    ref = live["ref"]
    writer._take(ref, {"type": "assistant.delta", "delta": "First"})
    writer._flush(live)
    first = calls[-1][-1]["at"]
    writer._take(ref, {"type": "turn.completed"})
    writer._write(ref, live)
    for session, ref in ((live["session"], "next-floor-turn"), ("weave-other", "other-floor-turn")):
        sync.publish(session, ref, {"type": "turn.started"}, list, secrets("https://supabase.example"))
        writer._take(ref, {"type": "assistant.delta", "delta": ref})
        writer._flush(writer.live[ref])
    assert first < calls[-2][-1]["at"]
    assert first == calls[-1][-1]["at"]


def test_inactive_conversation_timestamp_floors_expire_without_reordering(quiet_writer, monkeypatch):
    writer, live, _calls = quiet_writer
    clock = [1760875200.123456]
    monkeypatch.setattr(sync.time, "time", lambda: clock[0])
    for i in range(10_000):
        writer._next_at({"session": f"weave-idle-{i}"})
    previous = writer._next_at(live)
    clock[0] += 24 * 60 * 60 + 1
    writer._next_at({"session": "weave-new"})
    assert len(writer.last_at_us) == 1
    assert live["session"] not in writer.last_at_us
    # Even delayed rows with old source timestamps sort after the evicted floor.
    assert sync._iso(writer._next_at(live, previous - 10)) > sync._iso(previous)
    assert len(writer.last_at_us) == 2


@pytest.mark.parametrize("text_arrives", [False, True])
def test_permanently_failed_status_cleanup_expires(quiet_writer, monkeypatch, caplog, text_arrives):
    writer, live, _calls = quiet_writer
    ref = live["ref"]
    clock, attempts, finished, committed = [0.0], [], [], []
    monkeypatch.setattr(sync.time, "monotonic", lambda: clock[0])
    caplog.set_level("ERROR", logger=sync.__name__)
    writer._take(ref, {"type": "tool.started", "tool_name": "browse"})
    writer._flush(live)
    if text_arrives:
        writer._take(ref, {"type": "assistant.delta", "delta": "Done"})
    writer._take(ref, {"type": "turn.completed", "_on_finished": lambda: finished.append(ref),
                       "_on_committed": lambda items: committed.append(items)})

    def offline(*_):
        attempts.append(clock[0])
        raise TimeoutError("permanently offline")

    monkeypatch.setattr(sync, "_call", offline)
    writer._write(ref, live)
    clock[0] = sync.FINAL_RETRY_S
    writer._write(ref, live)
    assert live.get("status_cleanup") and "read" not in live and "messages" not in live
    cleanup_start = clock[0]
    for _ in range(100):
        if ref not in writer.live:
            break
        clock[0] = min(live["retry_at"], cleanup_start + sync.FINAL_RETRY_S)
        writer._write(ref, live)
    assert ref not in writer.live, "permanent cleanup failure must release its credentials"
    assert clock[0] == cleanup_start + sync.FINAL_RETRY_S
    assert attempts[-1] < clock[0]  # deadline wins even if the next retry is due
    assert list(sync._ENDED) == [ref]
    assert finished == [ref] and not committed
    errors = [record.message for record in caplog.records if record.levelname == "ERROR"]
    assert sum("final write retries exhausted" in message for message in errors) == 1
    assert sum("status cleanup retries exhausted" in message for message in errors) == 1


@pytest.mark.parametrize("text_arrives", [False, True])
def test_status_cleanup_recovers_after_final_retry_expiry(quiet_writer, monkeypatch, text_arrives):
    writer, live, calls = quiet_writer
    ref = live["ref"]
    clock, finished, committed = [0.0], [], []
    monkeypatch.setattr(sync.time, "monotonic", lambda: clock[0])
    writer._take(ref, {"type": "tool.started", "tool_name": "browse"})
    writer._flush(live)
    writer._take(ref, {"type": "tool.completed"})
    writer._flush(live)
    status_id = writer._status_item_id(live)
    assert calls[-1][0]["body"]["status"] == "Thinking"
    if text_arrives:
        writer._take(ref, {"type": "assistant.delta", "delta": "Done"})
    writer._take(ref, {"type": "turn.completed", "_on_finished": lambda: finished.append(ref),
                       "_on_committed": lambda items: committed.append(items)})
    call = sync._call

    def offline(*_):
        raise TimeoutError("offline")

    monkeypatch.setattr(sync, "_call", offline)
    writer._write(ref, live)
    assert finished == [ref] and not committed
    clock[0] = sync.FINAL_RETRY_S + 1
    writer._write(ref, live)
    assert ref in writer.live, "the outstanding status retirement must not expire"
    assert live.get("status_cleanup") and "messages" not in live and "read" not in live
    assert finished == [ref] and not committed
    clock[0] += sync.FINAL_BACKOFF_S + 1
    writer._write(ref, live)  # cleanup itself can also fail; it must keep backing off
    assert ref in writer.live and live["retry_at"] > clock[0]
    monkeypatch.setattr(sync, "_call", call)
    conn = {key: live[key] for key in ("url", "key", "bearer", "session")}
    writer._take(ref, {"type": "out.rows", "_conn": conn,
                       "rows": [{"role": "assistant", "body": {"text": "Late file"}}]})
    assert calls[-1][-1]["body"]["text"] == "Late file"
    clock[0] += sync.FINAL_BACKOFF_S + 1
    writer._write(ref, live)
    assert ref not in writer.live
    status_writes = [row for batch in calls for row in batch if row.get("item_id") == status_id]
    assert status_writes[-1]["state"] == "removed" and status_writes[-1]["body"]["status"] is None
    assert finished == [ref] and not committed


@pytest.mark.parametrize("committed", [False, True])
def test_failed_first_status_write_is_retired_after_queued_write(quiet_writer, monkeypatch, committed):
    writer, live, calls = quiet_writer
    ref = live["ref"]
    status_id = writer._status_item_id(live)
    call = sync._call

    def timeout(conn, items):
        if committed:
            call(conn, items)  # the database committed but its acknowledgement was lost
        raise TimeoutError("status write timed out")

    monkeypatch.setattr(sync, "_call", timeout)
    writer._take(ref, {"type": "tool.started", "tool_name": "browse"})
    writer._write(ref, live)
    monkeypatch.setattr(sync, "_call", call)
    writer._take(ref, {"type": "assistant.delta", "delta": "Done", "message_uid": "retry-status-answer"})
    writer._write(ref, live)
    live["messages"] = [{"role": "assistant", "content": "Done", "message_uid": "retry-status-answer",
                         "timestamp": time.time()}]
    writer._take(ref, {"type": "turn.completed"})
    writer._write(ref, live)
    status_writes = [row for batch in calls for row in batch if row.get("item_id") == status_id]
    assert status_writes[-1]["state"] == "removed", status_writes
    assert status_writes[-1]["body"]["status"] is None
    assert ref not in writer.live


def test_finished_steps_are_turn_local_and_survive_replay(quiet_writer):
    writer, live, calls = quiet_writer
    messages = [
        {"role": "user", "content": "check", "platform_message_id": live["ref"]},
        {"role": "assistant", "content": "Checking", "message_uid": "comment", "tool_calls": [
            {"id": "a", "function": {"name": "web_search", "arguments": "secret arguments"}},
            {"id": "b", "function": {"name": "web_search", "arguments": "secret arguments"}}]},
        {"role": "tool", "tool_call_id": "a", "tool_name": "web_search", "content": '{"results": []}'},
        {"role": "tool", "tool_call_id": "b", "tool_name": "web_search", "content": '{"error": "secret failure"}'},
        {"role": "assistant", "content": "Done", "message_uid": "answer"},
    ]
    expected = [{"id": "a", "name": "web_search", "state": "completed"},
                {"id": "b", "name": "web_search", "state": "failed"}]
    for call_id, state in (("a", "completed"), ("b", "failed")):
        writer._take(live["ref"], {"type": "tool.started", "tool_call_id": call_id, "tool_name": "web_search"})
        writer._take(live["ref"], {"type": "tool." + state, "tool_call_id": call_id, "tool_name": "web_search"})
    live["messages"] = messages
    writer._take(live["ref"], {"type": "assistant.delta", "delta": "Done", "message_uid": "answer"})
    writer._take(live["ref"], {"type": "turn.completed"})
    writer._finish(live)
    assert calls[-1][-1]["body"].get("steps") == expected
    assert all("steps" not in row["body"] for row in calls[-1][:-1])
    assert sync.visible_rows(messages)[-1]["body"]["steps"] == expected
    assert sync.visible_rows(messages + [{"role": "user", "content": "hello"},
                                        {"role": "assistant", "content": "Hi", "message_uid": "plain"}])[-1]["body"].get("steps") is None
    assert "secret" not in json.dumps(calls)
    writer._finish(live)
    assert calls[-1][-1]["body"]["steps"] == expected


def test_finished_step_details_are_turn_local_with_reused_call_ids():
    messages = [
        {"role": "user", "content": "read first"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "reused", "function": {"name": "read_file", "arguments": json.dumps({"path": "first.txt"})}}]},
        {"role": "tool", "tool_call_id": "reused", "content": "ok", "display_metadata": {"duration_ms": 10}},
        {"role": "assistant", "content": "First answer", "message_uid": "first-answer"},
        {"role": "user", "content": "read second"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "reused", "function": {"name": "read_file", "arguments": json.dumps({"path": "second.txt"})}}]},
        {"role": "tool", "tool_call_id": "reused", "content": "ok", "display_metadata": {"duration_ms": 20}},
        {"role": "assistant", "content": "Second answer", "message_uid": "second-answer"},
    ]

    answers = [row for row in sync.visible_rows(messages) if row["role"] == "assistant"]
    assert [row["body"]["text"] for row in answers] == ["First answer", "Second answer"]
    assert [row["body"]["steps"] for row in answers] == [
        [{"id": "reused", "name": "read_file", "state": "completed", "preview": "first.txt", "duration_ms": 10}],
        [{"id": "reused", "name": "read_file", "state": "completed", "preview": "second.txt", "duration_ms": 20}],
    ]


@pytest.mark.parametrize("duration_metadata", [{"duration_ms": 500}, '{"duration_ms": 500}', None])
def test_finished_steps_carry_preview_and_duration_live_and_replay(quiet_writer, duration_metadata):
    writer, live, calls = quiet_writer
    messages = [
        {"role": "user", "content": "check", "platform_message_id": live["ref"]},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "function": {"name": "terminal", "arguments": json.dumps({"command": "git status"})}},
            {"id": "b", "function": {"name": "read_file", "arguments": json.dumps({"path": "src/app.ts"})}}]},
        {"role": "tool", "tool_call_id": "a", "content": "ok", "display_metadata": {"duration_ms": 1250}},
        {"role": "tool", "tool_call_id": "b", "content": "ok",
         **({"display_metadata": duration_metadata} if duration_metadata is not None else {})},
        {"role": "assistant", "content": "Done", "message_uid": "answer"},
    ]
    expected = [
        {"id": "a", "name": "terminal", "state": "completed", "preview": "git", "duration_ms": 1250},
        {"id": "b", "name": "read_file", "state": "completed", "preview": "app.ts",
         **({"duration_ms": 500} if duration_metadata is not None else {})},
    ]
    for call_id, name in (("a", "terminal"), ("b", "read_file")):
        writer._take(live["ref"], {"type": "tool.started", "tool_call_id": call_id, "tool_name": name})
        writer._take(live["ref"], {"type": "tool.completed", "tool_call_id": call_id, "tool_name": name})
    live["messages"] = messages
    writer._take(live["ref"], {"type": "assistant.delta", "delta": "Done", "message_uid": "answer"})
    writer._take(live["ref"], {"type": "turn.completed"})
    writer._finish(live)
    assert calls[-1][-1]["body"]["steps"] == expected
    assert sync.visible_rows(messages)[-1]["body"]["steps"] == expected


@pytest.mark.parametrize("metadata", [
    {"duration_ms": -1}, {"duration_ms": True}, {"duration_ms": "12"},
    {"duration_ms": 1.5}, "not JSON",
])
def test_finished_steps_reject_invalid_duration_live_and_replay(quiet_writer, metadata):
    writer, live, calls = quiet_writer
    messages = [
        {"role": "user", "content": "check", "platform_message_id": live["ref"]},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "function": {"name": "terminal", "arguments": '{"command":"git"}'}}]},
        {"role": "tool", "tool_call_id": "a", "content": "ok", "display_metadata": metadata},
        {"role": "assistant", "content": "Done", "message_uid": "answer"},
    ]
    live["messages"] = messages
    writer._take(live["ref"], {"type": "turn.completed"})
    writer._write(live["ref"], live)
    expected = [{"id": "a", "name": "terminal", "state": "completed", "preview": "git"}]
    assert calls[-1][-1]["body"]["steps"] == expected
    assert sync.visible_rows(messages)[-1]["body"]["steps"] == expected


def test_writer_redacts_previews_in_publishing_profile(quiet_writer, tmp_path, monkeypatch):
    import hermes_constants
    from agent.redact import clear_vault_redaction_values, register_vault_redaction_value
    from gateway.weave_step_preview import step_preview

    writer, _live, calls = quiet_writer
    home_a, home_b = tmp_path / "A", tmp_path / "B"
    secret = "Zq9secretVault77"
    messages = [
        {"role": "user", "content": "check", "platform_message_id": "profile-B"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "function": {"name": "terminal", "arguments": json.dumps({"command": secret})}}]},
        {"role": "tool", "tool_call_id": "a", "content": "ok"},
        {"role": "assistant", "content": "Done", "message_uid": "profile-answer"},
    ]
    token_b = hermes_constants.set_hermes_home_override(home_b)
    try:
        register_vault_redaction_value(secret)
        expected = step_preview("terminal", {"command": secret})
        assert expected and secret not in expected
        assert sync.publish("weave-profile", "profile-B", {"type": "turn.completed"},
                            lambda: messages, secrets("https://supabase.example"))
        live = writer.live["profile-B"]
        writer._take(*writer.events.get_nowait())
        seen_homes = []
        real_finish = writer._finish

        def finish(entry):
            seen_homes.append(hermes_constants.get_hermes_home())
            return real_finish(entry)

        monkeypatch.setattr(writer, "_finish", finish)
        token_a = hermes_constants.set_hermes_home_override(home_a)
        try:
            assert step_preview("terminal", {"command": secret}) == secret
            writer._write("profile-B", live)  # real dispatch, not a direct _finish call
            assert hermes_constants.get_hermes_home() == home_a
        finally:
            hermes_constants.reset_hermes_home_override(token_a)
        assert calls[-1][-1]["body"]["steps"][0]["preview"] == expected
        assert seen_homes == [home_b]
    finally:
        clear_vault_redaction_values()
        hermes_constants.reset_hermes_home_override(token_b)


def test_finished_steps_stop_compaction_and_bounds(quiet_writer):
    writer, live, calls = quiet_writer
    ref = live["ref"]
    writer._take(ref, {"type": "tool.generating", "tool_name": "not_a_call"})
    writer._take(ref, {"type": "tool.started", "tool_call_id": "open", "tool_name": "read_file"})
    writer._take(ref, {"type": "assistant.delta", "delta": "Partial", "message_uid": "partial"})
    writer._take(ref, {"type": "turn.failed", "stopped": True})
    writer._finish(live)
    assert calls[-1][-1]["body"]["steps"] == [{"id": "open", "name": "read_file", "state": "started"}]
    call = {"role": "assistant", "content": "", "tool_calls": [{"id": "one", "function": {"name": "read_file"}}]}
    result = {"role": "tool", "tool_call_id": "one", "content": "ok"}
    answer = {"role": "assistant", "content": "Done", "message_uid": "done"}
    hidden = {"role": "user", "content": "", "display_kind": "hidden"}
    replay = sync.visible_rows([call, result, hidden, call, result, answer])
    assert replay[-1]["body"]["steps"] == [{"id": "one", "name": "read_file", "state": "completed"}]
    many = [{"id": str(i), "function": {"name": "read_file"}} for i in range(102)]
    bounded = sync.visible_rows([{**call, "tool_calls": many}, answer])[-1]["body"]
    assert len(bounded["steps"]) == 100 and bounded["steps_truncated"] is True
    assert len(json.dumps(bounded).encode()) < 131_072
    assert sync.visible_rows([call, {"role": "user", "content": "steer"}, answer])[-1]["body"].get("steps") is None


@pytest.mark.parametrize("event_ids", [("before", "after"), ("after",), ("after", "before")])
@pytest.mark.parametrize("steer_text", ["Use a different approach", "[OUT-OF-BAND user steer"])
def test_finished_steps_steer_live_matches_replay_in_call_order(quiet_writer, steer_text, event_ids):
    writer, live, calls = quiet_writer
    messages = [{"role": "user", "content": "Check", "platform_message_id": live["ref"]}]
    for call_id in ("before", "after"):
        if call_id == "after":
            # The display kind is authoritative even without the model-facing marker.
            messages.append({"role": "user", "content": steer_text, "display_kind": "steer"})
        messages.extend([
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": call_id, "function": {"name": "read_file"}}]},
            {"role": "tool", "tool_call_id": call_id, "content": "ok"},
        ])
    for call_id in event_ids:
        writer._take(live["ref"], {"type": "tool.completed", "tool_call_id": call_id, "tool_name": "read_file"})
    messages.append({"role": "assistant", "content": "Done", "message_uid": "steered"})
    live["messages"] = messages
    writer._take(live["ref"], {"type": "turn.completed"})
    writer._finish(live)
    expected = [{"id": call_id, "name": "read_file", "state": "completed"} for call_id in ("before", "after")]
    assert calls[-1][-1]["body"]["steps"] == expected
    assert sync.visible_rows(messages)[-1]["body"]["steps"] == expected


@pytest.mark.parametrize("plain_followup", [False, True])
def test_finished_steps_internal_admission_splits_full_history(plain_followup):
    first = [
        {"role": "user", "content": "Check"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "function": {"name": "read_file"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "ok"},
        {"role": "assistant", "content": "First", "message_uid": "first"},
    ]
    internal = [{"role": "user", "content": "[IMPORTANT: Background task finished]",
                 "display_kind": "internal_notification"}]
    if not plain_followup:
        internal.extend([
            {"role": "assistant", "content": "", "tool_calls": [{"id": "b", "function": {"name": "read_file"}}]},
            {"role": "tool", "tool_call_id": "b", "content": "ok"},
        ])
    internal.append({"role": "assistant", "content": "Second", "message_uid": "second"})
    assert sync.visible_rows(first + internal) == sync.visible_rows(first) + sync.visible_rows(internal)
    assert all(row["body"]["text"] != internal[0]["content"] for row in sync.visible_rows(first + internal))


@pytest.mark.parametrize("payload_kind", ["marker", "durable", "skipped", "keyboard_interrupt"])
def test_finished_steps_cancelled_payload_live_and_replay(quiet_writer, payload_kind):
    from agent.tool_executor import _ToolCancelledResult

    writer, live, calls = quiet_writer
    cancelled = "[Tool execution cancelled — read_file was abandoned: user stop]"
    payload = {
        "marker": _ToolCancelledResult(cancelled),
        "durable": cancelled,
        "skipped": "[Tool execution cancelled — read_file was skipped. User sent a new message]",
        "keyboard_interrupt": json.dumps({"error": "Tool execution cancelled. User stop", "status": "cancelled"}),
    }[payload_kind]
    messages = [
        {"role": "user", "content": "Read", "platform_message_id": live["ref"]},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "done", "function": {"name": "read_file"}},
            {"id": "cancelled", "function": {"name": "read_file"}}]},
        {"role": "tool", "tool_call_id": "done", "content": "ok"},
        {"role": "tool", "tool_call_id": "cancelled", "content": payload},
        {"role": "assistant", "content": "Partial", "message_uid": "partial"},
    ]
    live["messages"] = messages
    # The native binary classifier currently emits completed for the bracketed cancellation.
    for call_id in ("done", "cancelled"):
        writer._take(live["ref"], {"type": "tool.started", "tool_call_id": call_id, "tool_name": "read_file"})
        writer._take(live["ref"], {"type": "tool.completed", "tool_call_id": call_id, "tool_name": "read_file"})
    writer._take(live["ref"], {"type": "assistant.delta", "delta": "Partial", "message_uid": "partial"})
    writer._take(live["ref"], {"type": "turn.failed", "stopped": True})
    writer._finish(live)
    expected = [{"id": "done", "name": "read_file", "state": "completed"},
                {"id": "cancelled", "name": "read_file", "state": "started"}]
    assert calls[-1][-1]["body"]["steps"] == expected
    assert sync.visible_rows(messages)[-1]["body"]["steps"] == expected


def test_finished_steps_domain_cancelled_status_is_a_completed_call():
    order = json.dumps({"success": True, "status": "cancelled", "order_id": "order-17"})
    messages = [
        {"role": "user", "content": "Cancel my order"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "order", "function": {"name": "cancel_order"}}]},
        {"role": "tool", "tool_call_id": "order", "content": order},
        {"role": "assistant", "content": "Cancelled", "message_uid": "done"},
    ]
    assert sync.visible_rows(messages)[-1]["body"]["steps"] == [
        {"id": "order", "name": "cancel_order", "state": "completed"}]


def test_finished_steps_scaffolding_keeps_the_same_turn():
    messages = [{"role": "assistant", "content": "", "tool_calls": [{"id": "a", "function": {"name": "read_file"}}]},
                {"role": "tool", "tool_call_id": "a", "content": "ok"}]
    messages.extend({"role": "user", "content": prefix + " context"} for prefix in sync.SCAFFOLDING)
    messages.extend([{"role": "user", "content": "summary", "display_kind": "hidden"},
                     {"role": "assistant", "content": "Done", "message_uid": "done"}])
    assert sync.visible_rows(messages)[-1]["body"]["steps"] == [{"id": "a", "name": "read_file", "state": "completed"}]


def test_finished_steps_rebuilt_from_compacted_sqlite_transcript(tmp_path):
    from hermes_state import SessionDB
    from gateway.platforms.api_server import _project_client_message

    db = SessionDB(tmp_path / "steps.db")
    try:
        sid = db.create_session("steps", "api_server")
        db.append_message(sid, "user", "Check")
        db.append_message(sid, "assistant", "Checking", tool_calls=[
            {"id": "one", "function": {"name": "read_file", "arguments": "private"}}])
        db.append_message(sid, "tool", "ok", tool_call_id="one", tool_name="read_file")
        db.append_message(sid, "assistant", "Done")
        def replay():
            return sync.visible_rows([_project_client_message(m) for m in db.get_messages(sid, include_compacted=True, include_ancestors=True)])
        before = replay()
        db.archive_and_compact(sid, [{"role": "user", "content": "summary", "display_kind": "hidden"}])
        after = replay()
        answer = next(row for row in after if row["body"].get("text") == "Done")
        assert answer == before[-1]
        assert answer["body"]["steps"] == [{"id": "one", "name": "read_file", "state": "completed"}]
    finally:
        db.close()


def test_show_tag_marks_a_reply_visible_and_leaves_the_tag_out():
    """DEC-415: the model decides what the screen shows on a call; the tag never reaches the row."""
    assert sync.visible_reply("[SHOW] Here is the table.") == ("Here is the table.", True)
    assert sync.visible_reply("  [SHOW]\nTwo lines") == ("Two lines", True)
    assert sync.visible_reply("Sure, lunch is at one.") == ("Sure, lunch is at one.", False)
    assert sync.visible_reply("See https://example.com") == ("See https://example.com", True)
    assert sync.visible_reply("x" * 401) == ("x" * 401, True)
    assert sync.visible_reply("[READ] ok", show_tag="[READ]", show_over=5) == ("ok", True)
    assert sync.visible_reply("short", show_tag="[READ]", show_over=3) == ("short", True)
    rows = sync.visible_rows([{"role": "user", "content": "[SHOW] not for user rows", "timestamp": 1.0},
                              {"role": "assistant", "content": "[SHOW] shown", "timestamp": 2.0},
                              {"role": "assistant", "content": "spoken", "timestamp": 3.0}])
    assert [r["body"] for r in rows] == [{"text": "[SHOW] not for user rows"}, {"text": "shown", "visible": True},
                                         {"text": "spoken", "visible": False}]


def test_show_settings_come_from_the_profile_secrets(server, monkeypatch):
    url, calls, _rows = server
    now = time.time()
    values = {"WEAVE_CHAT_SUPABASE_URL": url.replace("http://", "https://"), "WEAVE_CHAT_SUPABASE_KEY": "k",
              "WEAVE_API_MCP_BEARER": "b", "WEAVE_CHAT_SHOW_TAG": "[READ]", "WEAVE_CHAT_SHOW_OVER_CHARS": "nope"}
    real = sync._call
    monkeypatch.setattr(sync, "_call", lambda live, items: real({**live, "url": live["url"].replace("https://", "http://")}, items))
    saved = [{"id": 1, "role": "assistant", "content": "[READ] read me", "timestamp": now + 1.0}]
    for event in ({"type": "assistant.delta", "delta": "[READ] read me"}, {"type": "turn.completed"}):
        sync.publish("weave-show", "ref-show", event, lambda: saved, lambda name: values.get(name, ""))
    wait_for(lambda: any(i.get("state") == "final" for c in calls for i in c["p_items"]) and "ref-show" not in (sync._WRITER.live if sync._WRITER else {}))
    final = next(i for c in calls for i in c["p_items"] if i.get("state") == "final")
    assert (final["body"]["text"], final["body"]["visible"]) == ("read me", True)


def test_ledger_vault_card_keeps_always_on_server(server, monkeypatch):
    url, calls, rows = server
    approval = "0199dddd-0000-7000-8000-000000000001"
    run_turn(url, "weave-abc", "vault-ref", [
        {"type": "approval.request", "request_id": "vault-waiter", "pattern_key": "plugin_rule:" + approval,
         "description": "Use saved login", "choices": ["once", "deny"]},
        {"type": "turn.completed"},
    ], [], monkeypatch)
    wait_for(lambda: rows.get("approval:vault-waiter", {}).get("state") == "resolved")
    card = next(i for c in calls for i in c["p_items"] if i.get("cell_key") == "approval:vault-waiter")
    assert card["body"]["approval_ref"] == approval
    assert card["body"]["choices"] == ["once", "always", "deny"]
    assert card["body"]["native_request_ref"] == "vault-ref"


def test_a_turn_writes_only_its_own_rows_however_long_the_chat(server, monkeypatch):
    """A finished turn writes the rows from its own user message on (Hermes stores the turn ref on it), never the
    history before it: re-sending old rows re-stamped every one and flooded the apps and the call (2026-10-16)."""
    url, calls, rows = server
    now = time.time()
    history = [{"id": i, "role": "user" if i % 2 else "assistant", "content": f"old {i}", "timestamp": now - 9000 + i,
                "platform_message_id": f"old-{i}"} for i in range(150)]
    saved = history + [{"id": 900, "role": "user", "content": "open redmart", "timestamp": now - 30, "platform_message_id": "ref-long"},
                       {"id": 901, "role": "assistant", "content": "Opening it.", "timestamp": now + 1}]
    run_turn(url, "weave-abc", "ref-long", [{"type": "assistant.delta", "delta": "Opening it."}, {"type": "turn.completed"}],
             saved, monkeypatch)
    live_id = str(sync.uuid.uuid5(sync.uuid.NAMESPACE_URL, "weave-chat:ref-long"))
    wait_for(lambda: rows.get(live_id, {}).get("state") == "final")
    finals = [i.get("cell_key") for c in calls for i in c["p_items"] if i.get("state") == "final"]
    assert finals == [f"user:{now - 30!r}", f"assistant:{now + 1!r}"]  # a user row from before turn start still counts


def test_micro_center_turn_with_browser_card_and_message_uids_keeps_exact_order(server, monkeypatch):
    """WEV-3483: a browser card passive append arriving while commentary is buffered flushes the commentary
    first, never duplicates the second update, and keeps every row in causal created_at order."""
    url, _calls, rows = server
    real = sync._call
    monkeypatch.setattr(sync, "_call", lambda live, items: real({**live, "url": live["url"].replace("https://", "http://")}, items))
    t0 = 1792224000.4678
    u1 = "The browser didn't open in a new window. I'll retry in the current window."
    card_json = '{"card_id":"98d1d290","kind":"browser","major":1,"page_url":"https://www.microcenter.com/"}'
    u2 = "The Micro Center page is showing a Cloudflare security check. I'll wait a moment."
    ans = "I opened microcenter.com in your browser, but it's stuck on a Cloudflare page."
    saved = [
        {"role": "user", "content": "Open micro center website in browser.", "timestamp": t0, "platform_message_id": "ref-mc"},
        {"role": "assistant", "content": "", "tool_calls": [{}], "timestamp": t0 + 6, "message_uid": "u-empty"},
        {"role": "assistant", "content": u1, "tool_calls": [{}], "timestamp": t0 + 26, "message_uid": "u-1"},
        {"role": "assistant", "content": card_json, "timestamp": t0 + 30, "message_uid": "u-card"},
        {"role": "assistant", "content": u2, "tool_calls": [{}], "timestamp": t0 + 32, "message_uid": "u-2"},
        {"role": "assistant", "content": ans, "timestamp": t0 + 36, "message_uid": "u-ans"},
    ]
    sec = secrets(url)
    sync.publish("weave-mc", "ref-mc", {"type": "tool.started", "tool_name": "browse"}, lambda: saved, sec)
    sync.publish("weave-mc", "ref-mc", {"type": "tool.failed", "tool_name": "browse"}, lambda: saved, sec)
    sync.publish("weave-mc", "ref-mc", {"type": "assistant.delta", "delta": u1, "message_uid": "u-1", "at": t0 + 26}, lambda: saved, sec)
    sync.publish("weave-mc", "ref-mc", {"type": "tool.started", "tool_name": "browse"}, lambda: saved, sec)
    # Browser card arrives via publish_rows while the turn is running (even before FLUSH_S ticks).
    assert sync.publish_rows("weave-mc", [saved[3]], sec)
    sync.publish("weave-mc", "ref-mc", {"type": "tool.completed", "tool_name": "browse"}, lambda: saved, sec)
    sync.publish("weave-mc", "ref-mc", {"type": "assistant.delta", "delta": "\n\n" + u2, "message_uid": "u-2", "at": t0 + 32}, lambda: saved, sec)
    sync.publish("weave-mc", "ref-mc", {"type": "tool.started", "tool_name": "browse"}, lambda: saved, sec)
    sync.publish("weave-mc", "ref-mc", {"type": "tool.completed", "tool_name": "browse"}, lambda: saved, sec)
    sync.publish("weave-mc", "ref-mc", {"type": "assistant.delta", "delta": ans, "message_uid": "u-ans", "at": t0 + 36}, lambda: saved, sec)
    sync.publish("weave-mc", "ref-mc", {"type": "turn.completed"}, lambda: saved, sec)

    wait_for(lambda: "ref-mc" not in (sync._WRITER.live if sync._WRITER else {}))
    shown = [r["text"].strip() for r in sorted(rows.values(), key=lambda r: (r["created_at"], r.get("item_id") or ""))
             if r.get("state") != "removed"]
    assert shown == ["Open micro center website in browser.", u1, card_json, u2, ans]


def test_shared_file_row_flushes_buffered_commentary_first(server, monkeypatch):
    """WEV-3483: a shared file row routed through publish_rows(raw_rows=...) during a turn flushes buffered
    commentary before writing the file row, with monotonic created_at timestamps."""
    url, _calls, rows = server
    real = sync._call
    monkeypatch.setattr(sync, "_call", lambda live, items: real({**live, "url": live["url"].replace("https://", "http://")}, items))
    t0 = 1792225000.0
    saved = [
        {"role": "user", "content": "Send the PDF", "timestamp": t0, "platform_message_id": "ref-file"},
        {"role": "assistant", "content": "Uploading the report now.", "tool_calls": [{}], "timestamp": t0 + 1, "message_uid": "u-pre"},
        {"role": "assistant", "content": "Sent the report.", "timestamp": t0 + 3, "message_uid": "u-post"},
    ]
    sec = secrets(url)
    sync.publish("weave-file", "ref-file", {"type": "assistant.delta", "delta": "Uploading the report now.", "message_uid": "u-pre", "at": t0 + 1}, lambda: saved, sec)
    sync.publish("weave-file", "ref-file", {"type": "tool.started", "tool_name": "share_file"}, lambda: saved, sec)
    file_row = {"item_id": "0199file-0000-7000-8000-000000000001", "role": "assistant", "kind": "text", "state": "final",
                "body": {"text": "Report.pdf", "attachments": [{"path": "c1/i1/Report.pdf", "media_type": "application/pdf"}]}}
    assert sync.publish_rows("weave-file", [], sec, raw_rows=[file_row])
    sync.publish("weave-file", "ref-file", {"type": "tool.completed", "tool_name": "share_file"}, lambda: saved, sec)
    sync.publish("weave-file", "ref-file", {"type": "assistant.delta", "delta": "Sent the report.", "message_uid": "u-post", "at": t0 + 3}, lambda: saved, sec)
    sync.publish("weave-file", "ref-file", {"type": "turn.completed"}, lambda: saved, sec)

    wait_for(lambda: "ref-file" not in (sync._WRITER.live if sync._WRITER else {}))
    shown = [r["text"].strip() for r in sorted(rows.values(), key=lambda r: (r["created_at"], r.get("item_id") or ""))
             if r.get("state") != "removed"]
    assert shown == ["Send the PDF", "Uploading the report now.", "Report.pdf", "Sent the report."]


def test_tool_status_before_browser_card_before_answer_keeps_card_above_answer(server, monkeypatch):
    """WEV-3483 (12:16 SGT shape): tool.started flushes a status-only row before any text; then a browser card
    arrives, then the answer streams. The status row is removed so the card stays above the answer."""
    url, calls, rows = server
    real = sync._call
    monkeypatch.setattr(sync, "_call", lambda live, items: real({**live, "url": live["url"].replace("https://", "http://")}, items))
    t0 = 1792210562.7432
    card_json = '{"card_id":"c716beba","kind":"browser","major":1,"page_url":"https://www.microcenter.com/"}'
    ans = "I couldn't get the Micro Center homepage to load. Cloudflare is showing a check."
    saved = [
        {"role": "user", "content": "Open micro center website in the browser", "timestamp": t0, "platform_message_id": "ref-1216"},
        {"role": "assistant", "content": "", "tool_calls": [{}], "timestamp": t0 + 4},
        {"role": "assistant", "content": card_json, "timestamp": t0 + 9, "message_uid": "u-card1"},
        {"role": "assistant", "content": "", "tool_calls": [{}], "timestamp": t0 + 13},
        {"role": "assistant", "content": ans, "timestamp": t0 + 24, "message_uid": "u-ans1"},
    ]
    sec = secrets(url)
    sync.publish("weave-1216", "ref-1216", {"type": "tool.started", "tool_name": "browse"}, lambda: saved, sec)
    wait_for(lambda: len(calls) >= 1)  # status-only row flushed to Supabase first
    assert sync.publish_rows("weave-1216", [saved[2]], sec)
    sync.publish("weave-1216", "ref-1216", {"type": "tool.completed", "tool_name": "browse"}, lambda: saved, sec)
    sync.publish("weave-1216", "ref-1216", {"type": "assistant.delta", "delta": ans, "message_uid": "u-ans1", "at": t0 + 24}, lambda: saved, sec)
    sync.publish("weave-1216", "ref-1216", {"type": "turn.completed"}, lambda: saved, sec)

    wait_for(lambda: "ref-1216" not in (sync._WRITER.live if sync._WRITER else {}))
    shown = [r["text"].strip() for r in sorted(rows.values(), key=lambda r: (r["created_at"], r.get("item_id") or ""))
             if r.get("state") != "removed"]
    assert shown == ["Open micro center website in the browser", card_json, ans]


def test_identical_commentary_and_retry_supersede_match_by_message_uid(server, monkeypatch):
    """WEV-3483: two identical commentary messages keep their own rows by message_uid, and an aborted stream
    attempt whose message_uid is not saved in state.db is marked removed."""
    url, _calls, rows = server
    real = sync._call
    monkeypatch.setattr(sync, "_call", lambda live, items: real({**live, "url": live["url"].replace("https://", "http://")}, items))
    t0 = 1792230000.0
    same = "Still waiting for the page to settle."
    ans = "Done."
    saved = [
        {"role": "user", "content": "Check status", "timestamp": t0, "platform_message_id": "ref-dup"},
        {"role": "assistant", "content": same, "tool_calls": [{}], "timestamp": t0 + 1, "message_uid": "u-first"},
        {"role": "assistant", "content": same, "tool_calls": [{}], "timestamp": t0 + 3, "message_uid": "u-second"},
        {"role": "assistant", "content": ans, "timestamp": t0 + 5, "message_uid": "u-final"},
    ]
    sec = secrets(url)
    # Stream first identical commentary
    sync.publish("weave-dup", "ref-dup", {"type": "assistant.delta", "delta": same, "message_uid": "u-first", "at": t0 + 1}, lambda: saved, sec)
    sync.publish("weave-dup", "ref-dup", {"type": "tool.started", "tool_name": "browse"}, lambda: saved, sec)
    sync.publish("weave-dup", "ref-dup", {"type": "tool.completed", "tool_name": "browse"}, lambda: saved, sec)
    # Aborted stream attempt (u-aborted never saved to state.db) followed by retry (u-second)
    sync.publish("weave-dup", "ref-dup", {"type": "assistant.delta", "delta": "Half stream...", "message_uid": "u-aborted", "at": t0 + 2}, lambda: saved, sec)
    sync.publish("weave-dup", "ref-dup", {"type": "assistant.delta", "delta": same, "message_uid": "u-second", "at": t0 + 3}, lambda: saved, sec)
    sync.publish("weave-dup", "ref-dup", {"type": "tool.started", "tool_name": "browse"}, lambda: saved, sec)
    sync.publish("weave-dup", "ref-dup", {"type": "tool.completed", "tool_name": "browse"}, lambda: saved, sec)
    sync.publish("weave-dup", "ref-dup", {"type": "assistant.delta", "delta": ans, "message_uid": "u-final", "at": t0 + 5}, lambda: saved, sec)
    sync.publish("weave-dup", "ref-dup", {"type": "turn.completed"}, lambda: saved, sec)

    wait_for(lambda: "ref-dup" not in (sync._WRITER.live if sync._WRITER else {}))
    shown = [r["text"].strip() for r in sorted(rows.values(), key=lambda r: (r["created_at"], r.get("item_id") or ""))
             if r.get("state") != "removed"]
    assert shown == ["Check status", same, same, ans]


def test_stream_delivery_mints_message_uid_and_build_assistant_message_consumes_it():
    """WEV-3483: StreamDeliveryMixin mints (message_uid, timestamp) at first delta and build_assistant_message
    stamps that exact pair onto the saved assistant message dict."""
    from types import SimpleNamespace
    from agent.chat_completion_helpers import build_assistant_message
    from agent.stream_delivery import StreamDeliveryMixin

    class DummyAgent(StreamDeliveryMixin):
        def __init__(self):
            self.quiet_mode = False
            self.show_commentary = True
            self.session_id = "weave-test"
            self.model = "m"
            self.provider = "p"
            self.platform = "api_server"
            self._stream_callback = None
            self.stream_delta_callback = None
            self.interim_assistant_callback = None
            self.reasoning_callback = None
            self._reset_stream_delivery_tracking()

        def _needs_thinking_reasoning_pad(self):
            return False

        @staticmethod
        def _extract_reasoning(_msg):
            return None

        @staticmethod
        def _strip_think_blocks(text):
            return text

    agent = DummyAgent()
    captured = []
    agent.stream_delta_callback = lambda chunk: captured.append((chunk, agent._current_stream_message_uid, agent._current_stream_timestamp))
    agent._fire_stream_delta("Hello ")
    agent._fire_stream_delta("world")
    assert len(captured) == 2
    uid1, ts1 = captured[0][1], captured[0][2]
    assert isinstance(uid1, str) and len(uid1) == 32 and isinstance(ts1, float)
    assert captured[1][1] == uid1 and captured[1][2] == ts1

    sdk_msg = SimpleNamespace(content="Hello world", tool_calls=None, reasoning_content=None,
                              reasoning_details=None, codex_reasoning_items=None, codex_message_items=None)
    msg = build_assistant_message(agent, sdk_msg, "stop")
    assert msg["message_uid"] == uid1 and msg["timestamp"] == ts1
    assert agent._current_stream_message_uid is None and agent._current_stream_timestamp is None

    # A superseded stream attempt fires _stream_retract_callback with the aborted message_uid.
    retracted = []
    agent._stream_retract_callback = retracted.append
    agent._fire_stream_delta("Partial ")
    aborted_uid = agent._current_stream_message_uid
    assert isinstance(aborted_uid, str)
    agent._claim_stream_writer()
    assert retracted == [aborted_uid]


def test_dedicated_status_row_approval_order_and_single_final_revision(server, monkeypatch):
    """WEV-3483: (1) status uses a dedicated row so silent tool -> approval card -> answer keeps the answer after
    the approval card and adopts its message_uid; (2) completed commentary has identical body on close and finish
    so it never emits a second final revision; (3) superseded partial text is retracted, never published final."""
    url, calls, rows = server
    real = sync._call
    monkeypatch.setattr(sync, "_call", lambda live, items: real({**live, "url": live["url"].replace("https://", "http://")}, items))
    t0 = 1792240000.0
    saved = [
        {"role": "user", "content": "Buy it", "timestamp": t0, "platform_message_id": "ref-stat"},
        {"role": "assistant", "content": "Checking stock.", "tool_calls": [{}], "timestamp": t0 + 2, "message_uid": "u-comm"},
        {"role": "assistant", "content": "Purchased.", "timestamp": t0 + 10, "message_uid": "u-ans"},
    ]
    sec = secrets(url)
    # Silent tool status flushed first
    sync.publish("weave-stat", "ref-stat", {"type": "tool.started", "tool_name": "browse"}, lambda: saved, sec)
    wait_for(lambda: len(calls) >= 1)
    sync.publish("weave-stat", "ref-stat", {"type": "tool.completed", "tool_name": "browse"}, lambda: saved, sec)
    # Commentary streams and closes before approval tool
    sync.publish("weave-stat", "ref-stat", {"type": "assistant.delta", "delta": "Checking stock.", "message_uid": "u-comm", "at": t0 + 2}, lambda: saved, sec)
    sync.publish("weave-stat", "ref-stat", {"type": "tool.started", "tool_name": "purchase"}, lambda: saved, sec)
    sync.publish("weave-stat", "ref-stat", {"type": "approval.request", "request_id": "req-1", "description": "Approve purchase?"}, lambda: saved, sec)
    wait_for(lambda: any(r.get("cell_key") == "approval:req-1" for r in rows.values()))
    sync.publish("weave-stat", "ref-stat", {"type": "tool.completed", "tool_name": "purchase"}, lambda: saved, sec)
    # Aborted partial stream before real answer
    sync.publish("weave-stat", "ref-stat", {"type": "assistant.delta", "delta": "Wrong partial.", "message_uid": "u-bad", "at": t0 + 8}, lambda: saved, sec)
    wait_for(lambda: any(r.get("cell_key") == "msg:u-bad" for r in rows.values()))
    sync.publish("weave-stat", "ref-stat", {"type": "assistant.retract", "message_uid": "u-bad"}, lambda: saved, sec)
    sync.publish("weave-stat", "ref-stat", {"type": "assistant.delta", "delta": "Purchased.", "message_uid": "u-ans", "at": t0 + 10}, lambda: saved, sec)
    sync.publish("weave-stat", "ref-stat", {"type": "turn.completed"}, lambda: saved, sec)

    wait_for(lambda: "ref-stat" not in (sync._WRITER.live if sync._WRITER else {}))
    visible = [r.get("cell_key") for r in sorted(rows.values(), key=lambda r: (r["created_at"], r.get("item_id") or ""))
               if r.get("state") != "removed"]
    assert visible == [f"user:{t0!r}", "msg:u-comm", "approval:req-1", "msg:u-ans"]
    # Superseded partial was never sent with state == 'final'
    assert not [i for c in calls for i in c["p_items"] if i.get("cell_key") == "msg:u-bad" and i.get("state") == "final"]
    # Completed commentary emitted identical body on both close and finish
    comm_finals = [i["body"] for c in calls for i in c["p_items"] if i.get("cell_key") == "msg:u-comm" and i.get("state") == "final"]
    assert len(comm_finals) >= 1 and all(b == comm_finals[0] for b in comm_finals)


def test_publish_rows_delivers_even_after_queued_terminal(server, monkeypatch):
    """WEV-3483: if publish_rows queues an out.rows event right behind a queued turn.completed event, the writer
    still delivers the row using the attached connection fallback instead of dropping it."""
    url, _calls, rows = server
    real = sync._call
    monkeypatch.setattr(sync, "_call", lambda live, items: real({**live, "url": live["url"].replace("https://", "http://")}, items))
    t0 = 1792250000.0
    saved = [{"role": "user", "content": "hi", "timestamp": t0, "platform_message_id": "ref-late"},
             {"role": "assistant", "content": "done", "timestamp": t0 + 1, "message_uid": "u-done"}]
    sec = secrets(url)
    monkeypatch.setattr(sync, "FLUSH_S", 0.5)
    sync.publish("weave-late", "ref-late", {"type": "turn.completed"}, lambda: saved, sec)
    file_row = {"item_id": "0199late-0000-7000-8000-000000000001", "role": "assistant", "kind": "text", "state": "final",
                "body": {"text": "Late.pdf"}}
    assert sync.publish_rows("weave-late", [], sec, raw_rows=[file_row])
    wait_for(lambda: "0199late-0000-7000-8000-000000000001" in rows)
    assert rows["0199late-0000-7000-8000-000000000001"]["text"] == "Late.pdf"



def test_tool_status_flush_before_commentary_still_adopts_message_uid(server, monkeypatch):
    """When tool.started flushes a status row (setting live['rev']) before the commentary delta arrives,
    the row still adopts the delta's message_uid immediately and stamps cell_key='msg:<uid>' when closed."""
    url, calls, rows = server
    real = sync._call
    monkeypatch.setattr(sync, "_call", lambda live, items: real({**live, "url": live["url"].replace("https://", "http://")}, items))
    t0 = 1792231000.0
    saved = [
        {"role": "user", "content": "Run tool", "timestamp": t0, "platform_message_id": "ref-ts-uid"},
        {"role": "assistant", "content": "Commentary after status.", "tool_calls": [{}], "timestamp": t0 + 2, "message_uid": "u-comm"},
        {"role": "assistant", "content": "Final answer.", "timestamp": t0 + 4, "message_uid": "u-ans"},
    ]
    sec = secrets(url)
    sync.publish("weave-ts-uid", "ref-ts-uid", {"type": "tool.started", "tool_name": "browse"}, lambda: saved, sec)
    wait_for(lambda: len(calls) >= 1)
    sync.publish("weave-ts-uid", "ref-ts-uid", {"type": "assistant.delta", "delta": "Commentary after status.", "message_uid": "u-comm", "at": t0 + 2}, lambda: saved, sec)
    comm_id = sync._msg_item_id("u-comm")
    wait_for(lambda: rows.get(comm_id, {}).get("text") == "Commentary after status.")
    assert rows[comm_id].get("cell_key") == "msg:u-comm"
    assert sync._WRITER.live["ref-ts-uid"]["uid"] == "u-comm"
    sync.publish("weave-ts-uid", "ref-ts-uid", {"type": "tool.completed", "tool_name": "browse"}, lambda: saved, sec)
    sync.publish("weave-ts-uid", "ref-ts-uid", {"type": "assistant.delta", "delta": "Final answer.", "message_uid": "u-ans", "at": t0 + 4}, lambda: saved, sec)
    sync.publish("weave-ts-uid", "ref-ts-uid", {"type": "turn.completed"}, lambda: saved, sec)
    wait_for(lambda: "ref-ts-uid" not in (sync._WRITER.live if sync._WRITER else {}))
    assert rows[comm_id]["cell_key"] == "msg:u-comm" and rows[comm_id]["text"] == "Commentary after status."


def test_on_committed_fires_only_after_finish_succeeds(server, monkeypatch):
    """on_committed receives the committed item receipts only when _finish succeeds, while on_finished
    fires even on a failed write so the native awake lease is never held across retries."""
    url, _calls, _rows = server
    real = sync._call
    attempts = 0

    def flaky(live, items):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary supabase outage")
        return real({**live, "url": live["url"].replace("https://", "http://")}, items)

    monkeypatch.setattr(sync, "_call", flaky)
    monkeypatch.setattr(sync, "FINAL_BACKOFF_S", 0.05)
    t0 = 1792232000.0
    saved = [
        {"role": "user", "content": "Hello", "timestamp": t0, "platform_message_id": "ref-commit"},
        {"role": "assistant", "content": "World", "timestamp": t0 + 1, "message_uid": "u-world"},
    ]
    finished = []
    committed = []
    sync.publish("weave-commit", "ref-commit", {"type": "turn.completed"}, lambda: saved, secrets(url),
                 on_finished=lambda: finished.append(attempts),
                 on_committed=lambda items: committed.append(items))
    wait_for(lambda: len(finished) == 1)
    assert finished == [1]
    wait_for(lambda: len(committed) == 1)
    assert len(committed[0]) == 2 and all("item_id" in item for item in committed[0])



