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


def run_turn(url, session, ref, events, saved, monkeypatch):
    # The fake server is plain http; the writer only accepts https settings, so swap the scheme back at call time.
    real = sync._call
    monkeypatch.setattr(sync, "_call", lambda live, items: real({**live, "url": live["url"].replace("https://", "http://")}, items))
    for event in [{"type": "turn.started"}] + events:
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
    assert rows["clarify:c1"]["state"] == "resolved"


def test_off_without_settings_and_never_raises(server, monkeypatch):
    url, calls, _rows = server
    sync.publish("weave-abc", "ref3", {"type": "turn.started"}, lambda: [], lambda name: "")
    sync.publish("weave-abc", "ref3", {"type": "assistant.delta", "delta": "x"}, lambda: [], lambda name: "")
    sync.publish("not-weave", "ref4", {"type": "turn.started"}, lambda: [], secrets(url))
    time.sleep(0.2)
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
