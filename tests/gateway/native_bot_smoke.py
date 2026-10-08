"""Exercise real HTTP admission + bot messaging against a fixture model.

No provider/user credentials. This is transport/execution proof, not a live
Harso acceptance. Both agents are real Hermes; only inference is deterministic.
"""

import argparse, asyncio, json, os, pathlib, socket, subprocess, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ap = argparse.ArgumentParser()
ap.add_argument("fork")
ap.add_argument("--python", required=True)
a = ap.parse_args()
fork = pathlib.Path(a.fork).resolve()
root = pathlib.Path(tempfile.mkdtemp(prefix="native-wire-", dir=os.environ["TMPDIR"]))
home = root / "home"
home.mkdir()
requests = []


class Model(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps({
                "object": "list",
                "data": [{"id": "native-fixture", "object": "model"}],
            }).encode()
        )

    def do_POST(self):
        d = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        msgs = d.get("messages", [])
        text = "\n".join(str(m.get("content", "")) for m in msgs)
        has_tools = any(m.get("role") == "tool" for m in msgs)
        names = [t.get("function", {}).get("name") for t in d.get("tools", [])]
        is_helper = "Message from" in text and "HELPER_TASK_729" in text
        # Only the helper has this profile identity; the BEM prompt contains the
        # marker too, so distinguish by system instructions, not marker alone.
        is_helper = any(
            "NATIVE_WEM_IDENTITY_729" in str(m.get("content", ""))
            for m in msgs
            if m.get("role") == "system"
        )
        notification = (
            "Background process" in text and "NATIVE_HELPER_RESULT_729" in text
        )
        if "NATIVE_STOP_729" in text:
            time.sleep(1)
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_stop_" + str(len(requests)),
                        "type": "function",
                        "function": {"name": "fixture_pause", "arguments": "{}"},
                    }
                ],
            }
            finish = "tool_calls"
        elif is_helper:
            message = {"role": "assistant", "content": "NATIVE_HELPER_RESULT_729"}
            finish = "stop"
        elif notification:
            message = {"role": "assistant", "content": "NATIVE_BEM_RECEIVED_729"}
            finish = "stop"
        elif not has_tools and "message_agent" in names:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_native_729",
                        "type": "function",
                        "function": {
                            "name": "message_agent",
                            "arguments": json.dumps({
                                "target": "work",
                                "message": "HELPER_TASK_729: return your result.",
                            }),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": "NATIVE_BEM_DISPATCHED_729"}
            finish = "stop"
        requests.append({
            "helper": is_helper,
            "notification": notification,
            "finish": finish,
            "tools": names,
        })
        response = {
            "id": "fixture-" + str(len(requests)),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "native-fixture",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        if d.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            delta = dict(message)
            if "tool_calls" in delta:
                delta["tool_calls"] = [
                    dict(t, index=i) for i, t in enumerate(delta["tool_calls"])
                ]
            for part, end in [(delta, None), ({}, finish)]:
                chunk = {
                    "id": response["id"],
                    "object": "chat.completion.chunk",
                    "created": response["created"],
                    "model": "native-fixture",
                    "choices": [{"index": 0, "delta": part, "finish_reason": end}],
                }
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())


model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
threading.Thread(target=model.serve_forever, daemon=True).start()
config = {
    "model": {"default": "native-fixture", "provider": "custom:fixture"},
    "providers": {
        "fixture": {
            "api": f"http://127.0.0.1:{model.server_port}/v1",
            "api_key": "fixture-not-a-secret",
            "transport": "chat_completions",
        }
    },
    "platform_toolsets": {"cli": [], "tui": []},
    "gateway": {"native_bot_sessions": True},
    "agent": {"max_turns": 5},
    "terminal": {"backend": "local", "cwd": str(root)},
    "memory": {"memory_enabled": False, "user_profile_enabled": False},
    "compression": {"enabled": False},
    "security": {"tirith_enabled": False},
    "approvals": {"mode": "off"},
    "auxiliary": {"title": {"enabled": False}},
}
import yaml

for profile in ("general", "work"):
    p = home / "profiles" / profile
    p.mkdir(parents=True)
    (p / "config.yaml").write_text(yaml.safe_dump(config))
    (p / "profile.yaml").write_text(
        yaml.safe_dump({"ui_meta": {"hermes-bots": {"title": profile}}})
    )
    (p / "SOUL.md").write_text(
        "NATIVE_WEM_IDENTITY_729" if profile == "work" else "NATIVE_BEM_IDENTITY_729"
    )
    (p / ".env").write_text("API_SERVER_KEY=fixture-admission-key-not-secret\n")
    (p / ".no-bundled-skills").touch()
(home / "config.yaml").write_text(yaml.safe_dump(config))
(home / ".env").write_text("")
(home / ".no-bundled-skills").touch()
binpath = root / "bin"
binpath.mkdir()
launcher = binpath / "hermes"
import shlex

launcher.write_text(
    "#!/bin/sh\nexec " + shlex.quote(a.python) + ' -m hermes_cli.main "$@"\n'
)
launcher.chmod(0o700)
env = {
    k: v
    for k, v in os.environ.items()
    if not any(
        s in k.upper() for s in ("TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL")
    )
}
env.update(
    HOME=str(root),
    HERMES_HOME=str(home),
    HERMES_ROOT=str(home),
    PYTHONPATH=str(fork),
    PATH=str(binpath) + ":" + env["PATH"],
    TERMINAL_ENV="local",
    TMPDIR=str(root),
    HERMES_TESTING="1",
)
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
port = sock.getsockname()[1]
sock.close()
# Directly mount the documented, unchanged native WebSocket adapter. Loopback
# fixture only; production authentication is deliberately outside this proof.
server_code = 'from aiohttp import web\nfrom gateway.platforms.api_server import APIServerAdapter\nfrom gateway.config import GatewayConfig,PlatformConfig\nfrom gateway.run import GatewayRunner\nfrom types import SimpleNamespace\nimport hermes_state\nrunner=GatewayRunner(GatewayConfig(multiplex_profiles=True))\nrunner._running=True\nadapter=APIServerAdapter(PlatformConfig(enabled=True, extra={"key":"fixture-admission-key-not-secret"}))\nadapter.gateway_runner=runner\napp=web.Application(middlewares=[adapter._make_profile_prefix_middleware()])\nfor method,path,handler in adapter._http_route_table():\n app.router.add_route(method,path,handler)\n app.router.add_route(method,"/p/{profile}"+path,handler)\nweb.run_app(app,host="127.0.0.1",port=PORT,print=None)\n'.replace(
    "PORT", str(port)
)
entry = root / "serve.py"
entry.write_text(server_code)
log = (root / "server.log").open("w")
proc = subprocess.Popen(
    [a.python, str(entry)], cwd=root, env=env, stdout=log, stderr=log
)


async def run():
    import httpx

    start = time.monotonic()
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}/p/general",
        headers={"Authorization": "Bearer fixture-admission-key-not-secret"},
        timeout=40,
    ) as client:
        for _ in range(100):
            try:
                h = await client.get("/health")
                if h.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if proc.poll() is not None:
                raise RuntimeError("native server exited")
            await asyncio.sleep(0.1)
        sid = "weave-01964000-0000-7000-8000-000000000402"
        response = await client.post(
            "/api/sessions", json={"id": sid, "title": "Bot Chat"}
        )
        assert response.status_code == 201, (response.status_code, response.text)
        # Native key_cmd/profile auth owns inference. No legacy credential bind.
        denied = await client.post(f"/api/sessions/{sid}/submit", headers={"Authorization": "Bearer wrong"},
            json={"kind":"hermes.session.submit","external_request_id":"denied","message":"denied","busy_mode":"queue"})
        assert denied.status_code == 401
        response = await client.post(
            f"/api/sessions/{sid}/submit",
            json={
                "kind": "hermes.session.submit",
                "external_request_id": "fixture-request",
                "message": "Ask work to complete HELPER_TASK_729 then report its answer.",
                "busy_mode": "queue",
            },
        )
        assert response.status_code == 202, (response.status_code, response.text)
        deadline = time.monotonic() + 50
        payload = {}
        while time.monotonic() < deadline:
            response = await client.get(f"/api/sessions/{sid}/messages")
            payload = response.json()
            if "NATIVE_BEM_RECEIVED_729" in json.dumps(payload):
                break
            await asyncio.sleep(0.25)
        (root / "messages.json").write_text(json.dumps(payload, indent=2))
        (root / "model-calls.json").write_text(json.dumps(requests, indent=2))
        assert "NATIVE_BEM_RECEIVED_729" in json.dumps(payload), payload
        assert any(x["helper"] for x in requests) and any(
            x["notification"] for x in requests
        )
        # The observed Live failure: CLI completion left the durable row ended.
        # A new native turn resumes it without deleting or rotating the chat.
        ended = await client.patch(f"/api/sessions/{sid}", json={"end_reason": "cli_close"})
        assert ended.status_code == 200, ended.text
        # Older API callers still send a bind. Native inference must acknowledge
        # that obsolete envelope without rebinding/reopening the ended session.
        from datetime import datetime, timedelta, timezone
        old_bind = await client.post(f"/api/sessions/{sid}/credential/bind", json={
            "credential_slot": "GATE_B_API_KEY", "bearer": "obsolete-unused-bearer",
            "expires_at": (datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),
            "provider_route_revision_id": "obsolete-revision"})
        assert old_bind.status_code == 200, old_bind.text
        again = await client.post(f"/api/sessions/{sid}/submit", json={
            "kind": "hermes.session.submit", "external_request_id": "after-close",
            "message": "Confirm the completed helper result.", "busy_mode": "queue"})
        assert again.status_code == 202, again.text
        replay = await client.post(f"/api/sessions/{sid}/submit", json={
            "kind": "hermes.session.submit", "external_request_id": "after-close",
            "message": "Confirm the completed helper result.", "busy_mode": "queue"})
        assert replay.status_code == 202 and replay.json() == again.json()
        preserved = await client.get(f"/api/sessions/{sid}/messages")
        assert "NATIVE_BEM_RECEIVED_729" in preserved.text
        capability = (await client.get("/v1/capabilities")).json()
        assert capability["features"]["native_session_inference"] is True
        side = "weave-01964000-0000-7000-8000-000000000403"
        response = await client.post("/api/sessions", json={"id": side})
        assert response.status_code == 201
        slow = {
            "kind": "hermes.session.submit",
            "external_request_id": "slow-request",
            "message": "NATIVE_STOP_729",
            "busy_mode": "queue",
        }
        response = await client.post(f"/api/sessions/{side}/submit", json=slow)
        assert response.status_code == 202, response.text
        active = response.json()["native_request_ref"]
        control = {
            "kind": "hermes.session.submit",
            "external_request_id": "stop-request",
            "message": "Stop the previous task.",
            "busy_mode": "interrupt",
            "expected_active_ref": "wrong",
        }
        response = await client.post(f"/api/sessions/{side}/submit", json=control)
        assert response.status_code == 409, response.text

        async def terminal_event():
            async with client.stream(
                "GET", f"/api/sessions/{side}/submit/{active}/events"
            ) as stream:
                assert stream.status_code == 200
                text = (await stream.aread()).decode()
                assert "turn.failed" in text and "turn.completed" not in text, text

        reading = asyncio.create_task(terminal_event())
        await asyncio.sleep(0.1)
        control["expected_active_ref"] = active
        response = await client.post(f"/api/sessions/{side}/submit", json=control)
        assert response.status_code in (202, 503), response.text
        await asyncio.wait_for(reading, 10)
        response = await client.get(f"/api/sessions/{side}/submit/{active}/events")
        assert response.status_code == 409, response.text
        assert len([x for x in requests if x.get("finish") == "tool_calls"]) < 8
        response = await client.get(f"/api/sessions/{side}/messages")
        assert "NATIVE_BEM_RECEIVED_729" not in response.text
        print(
            json.dumps({
                "result": "passed",
                "proof": "real HTTP admission to native prompt and bot wake",
                "seconds": round(time.monotonic() - start, 2),
                "model_calls": len(requests),
                "artifact": str(root),
            })
        )


try:
    asyncio.run(run())
except Exception as e:
    print(
        json.dumps({
            "result": "failed",
            "error": type(e).__name__ + ": " + str(e),
            "artifact": str(root),
            "model_calls": requests,
        }),
        flush=True,
    )
    raise
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    model.shutdown()
    log.close()
