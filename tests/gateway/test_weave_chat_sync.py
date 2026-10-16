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
                key = item.get("cell_key") or item.get("item_id")
                row = rows.get(item.get("item_id")) or next((r for r in rows.values() if r.get("cell_key") == key), None)
                if row is None:
                    row = rows.setdefault(item.get("item_id") or key, {"rev": 0, "text": "", **item})
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
    monkeypatch.setattr(sync, "_SYNCED", set())
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
        {"id": 7, "role": "user", "content": "hello", "timestamp": now + 0.5},
        {"id": 8, "role": "assistant", "content": "", "tool_calls": [{"id": "t"}], "timestamp": now + 1.0},
        {"id": 9, "role": "tool", "content": "secret tool output", "timestamp": now + 1.5},
        {"id": 10, "role": "assistant", "content": "Hi there", "timestamp": now + 2.0},
        {"id": 11, "role": "user", "content": "[System: Continue now.]", "timestamp": now + 3.0},
    ]
    run_turn(url, "weave-abc", "ref1", [
        {"type": "assistant.delta", "delta": "Hi"},
        {"type": "tool.started", "tool_name": "web_search"},
        {"type": "tool.completed", "tool_name": "web_search"},
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
    assert set(finals) == {f"user:{now + 0.5!r}", f"assistant:{now - 500!r}", answer_key}
    assert finals[-1] == answer_key  # the turn's answer lands last: the apps settle on it


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
    saved = [{"id": 1, "role": "user", "content": "SQ 638?", "timestamp": now + 0.5},
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


def test_a_turn_without_streamed_text_still_ends_on_its_live_row(server, monkeypatch):
    url, _calls, rows = server
    now = time.time()
    saved = [{"id": 1, "role": "user", "content": "hi", "timestamp": now}, {"id": 2, "role": "assistant", "content": "Done.", "timestamp": now + 1}]
    run_turn(url, "weave-abc", "ref6", [{"type": "turn.completed"}], saved, monkeypatch)
    live_id = str(sync.uuid.uuid5(sync.uuid.NAMESPACE_URL, "weave-chat:ref6"))
    wait_for(lambda: rows.get(live_id, {}).get("state") == "final")
    assert rows[live_id]["text"] == "Done."


def test_an_immediate_turn_without_turn_started_still_streams(server, monkeypatch):
    # The gateway emits turn.started only for queued or steered submits; an immediate turn begins with its text.
    url, _calls, rows = server
    now = time.time()
    saved = [{"id": 1, "role": "user", "content": "hi", "timestamp": now}, {"id": 2, "role": "assistant", "content": "Hello", "timestamp": now + 1}]
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
    wait_for(lambda: any(i.get("state") == "final" for c in calls for i in c["p_items"]))
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
