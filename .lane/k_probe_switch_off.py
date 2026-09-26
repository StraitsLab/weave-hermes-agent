"""Switch-off byte-identity probe: one fixed transcript through a real AIAgent + Harso provider against a mock server.

Run with cwd = the tree under test (base worktree or K head). Prints canonical JSON of every request the agent sent
(chat completions + Harso endpoints). Throwaway HERMES_HOME. Usage: python k_probe_switch_off.py <out.json>
"""
import json, os, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

home = tempfile.mkdtemp(prefix="k-probe-home-", dir="/Users/abbhinnav/.hermes/cache/scratch")
os.environ["HERMES_HOME"] = home
os.environ.update({
    "WEAVE_HARSO_PROFILE_ID": "01990000-0000-7000-8000-000000000001",
    "WEAVE_HARSO_PROFILE_REVISION_ID": "01990000-0000-7000-8000-000000000002",
    "WEAVE_API_MCP_BEARER": "probe-bearer", "API_SERVER_KEY": "probe-route",
})
sys.path.insert(0, os.getcwd())

CAPTURED = []
QUEUE = []


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
        CAPTURED.append({"path": self.path, "body": body})
        if self.path.startswith("/internal/harso/context"):
            out = {"recall_status": "ok", "items": [{"citation": "[harso: e1]", "text": "User likes green tea."}]}
        elif self.path.startswith("/internal/harso/"):
            out = {"disposition": "admitted"}
        elif not self.path.endswith("/chat/completions"):
            out = {}
        elif body.get("stream") is True:
            msg = QUEUE.pop(0) if QUEUE else {"role": "assistant", "content": "DONE"}
            chunks = [{"id": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}]
            if msg.get("content"):
                chunks.append({"id": "m", "choices": [{"index": 0, "delta": {"content": msg["content"]}, "finish_reason": None}]})
            for i, tc in enumerate(msg.get("tool_calls") or []):
                chunks.append({"id": "m", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": i, "id": tc["id"], "type": "function", "function": tc["function"]}]}, "finish_reason": None}]})
            chunks.append({"id": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}]})
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
            return
        else:
            msg = QUEUE.pop(0) if QUEUE else {"role": "assistant", "content": "DONE"}
            out = {"id": "m", "choices": [{"index": 0, "message": msg,
                   "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}],
                   "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}}
        data = json.dumps(out).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]
os.environ["WEAVE_HARSO_ENDPOINT"] = f"http://127.0.0.1:{port}"

from pathlib import Path
from unittest.mock import patch
from hermes_state import SessionDB
from run_agent import AIAgent
from agent.memory_manager import MemoryManager
import plugins.memory.harso as harso

db = SessionDB(db_path=Path(home) / "state.db")
SID = "weave-01990000-0000-7000-8000-000000000003"


def make():
    a = AIAgent(api_key="k", base_url=f"http://127.0.0.1:{port}/v1", provider="openai-compat", model="test-model",
                max_iterations=6, enabled_toolsets=["file"], quiet_mode=True, skip_context_files=True, skip_memory=True,
                save_trajectories=False, platform="cli", session_db=db, session_id=SID)
    mm = MemoryManager()
    mm.add_provider(harso.HarsoMemoryProvider())
    mm.initialize_all(session_id=SID)
    a._memory_manager = mm
    pass
    return a


QUEUE.extend([
    {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function",
     "function": {"name": "read_file", "arguments": json.dumps({"file_path": "/nonexistent-k-probe"})}}]},
    {"role": "assistant", "content": "Here is the answer."},
])
with patch("hermes_cli.plugins.invoke_hook", side_effect=lambda hook, **kw: []):
    make().run_conversation("What tea do I like? Please check the file too.", conversation_history=[], task_id="t1")
    history = db.get_messages_as_conversation(SID)
    make().run_conversation("And what about coffee, remind me please?", conversation_history=history, task_id="t2")
    make()._memory_manager.flush_pending(5)


def canon(entry):
    body = dict(entry["body"])
    if isinstance(body.get("native_turn_ref"), str):  # random per-turn suffix (uuid) -> stable
        body["native_turn_ref"] = body["native_turn_ref"].rsplit(":", 1)[0] + ":<rand>"
    return {"path": entry["path"], "body": body}


print("ALL", [(e["path"], e["body"].get("stream"), len(e["body"].get("messages", []))) for e in CAPTURED])
reqs = [canon(e) for e in CAPTURED if e["path"] in ("/v1/chat/completions",) or e["path"].startswith("/internal/")]
json.dump(reqs, open(sys.argv[1], "w"), indent=1, sort_keys=True, ensure_ascii=False)
print(len(reqs), "requests;", sorted({r["path"] for r in reqs}))
srv.shutdown()
