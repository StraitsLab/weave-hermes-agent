"""WEV-3349: the cell writer streams a reply into Supabase chat_items and saves it as the same row."""
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
    assert final["body"] == {"text": "read me", "visible": True, "status": None}


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



