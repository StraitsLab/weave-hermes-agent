"""Loopback proof of Harso submit on the upstream gateway writer."""
import asyncio
import json
import os
from pathlib import Path
import sys
import threading
import traceback
from http.server import ThreadingHTTPServer
from http.server import BaseHTTPRequestHandler

class ModelPeer(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"data": [{"id": "local-wire-stub", "object": "model"}]}).encode())

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        last = next((m.get("content", "") for m in reversed(request.get("messages", []))
                     if m.get("role") == "user"), "")
        marker = "WS_SHARED" if "WS_SHARED" in json.dumps(last) else "MESSAGING_WARM"
        reply = f"LOCAL_ACK_{marker}"
        if 'BLOCK_FIFO' in json.dumps(last):
            self.server.blocked.set()
            assert self.server.release.wait(15), 'fixture model gate timed out'
        self.server.requests.append(request) if 'messages' in request else self.server.metadata_requests.append(request)
        base = {"id": "chatcmpl-local", "model": "local-wire-stub", "created": 1}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if request.get("stream") else "application/json")
        self.end_headers()
        if request.get("stream"):
            for delta, finish in [({"role": "assistant", "content": reply}, None), ({}, "stop")]:
                chunk = {**base, "object": "chat.completion.chunk", "choices": [
                    {"index": 0, "delta": delta, "finish_reason": finish}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self.wfile.write(json.dumps({**base, "object": "chat.completion", "choices": [
                {"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}).encode())



async def probe(peer):
    import aiohttp
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    from gateway.run import GatewayRunner
    runner = GatewayRunner()
    runner._running = True
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={'key': 'loopback-only-api', 'port': 0}))
    adapter.gateway_runner = runner
    adapter.set_message_handler(runner._handle_message)
    runner.adapters[Platform.API_SERVER] = adapter
    assert await adapter.connect()
    port = adapter._site._server.sockets[0].getsockname()[1]
    db = adapter._ensure_session_db()
    db.create_session('harso-native', 'api_server')
    headers = {'Authorization': 'Bearer loopback-only-api'}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as client:
            base = f'http://127.0.0.1:{port}/api/sessions/harso-native'
            body = {'kind': 'hermes.session.submit', 'external_request_id': 'first',
                    'message': 'HARSO_NATIVE', 'busy_mode': 'queue'}
            async with client.post(base + '/submit', json=body, headers=headers) as response:
                receipt = await response.json()
                assert response.status == 202, receipt
            ref = receipt['native_request_ref']
            async with client.get(base + f'/submit/{ref}/events', headers=headers) as response:
                events = await response.text()
                assert response.status == 200, events
                assert 'turn.completed' in events, events
            async with client.post(base + '/submit', json=body, headers=headers) as response:
                assert response.status == 202
                assert await response.json() == receipt
            rows = [m for m in db.get_messages('harso-native') if m['role'] == 'user']
            assert len(rows) == 1, rows
            assert len(peer.requests) == 1, peer.requests
            assert db.get_messages('harso-native')[0].get('display_kind') is None
            body = {**body, 'external_request_id': 'internal', 'message': 'HARSO_NOTICE', 'internal': True}
            async with client.post(base + '/submit', json=body, headers=headers) as response:
                notice = await response.json()
                assert response.status == 202, notice
            async with client.get(base + f'/submit/{notice["native_request_ref"]}/events', headers=headers) as response:
                assert 'turn.completed' in await response.text()
            users = [m for m in db.get_messages('harso-native') if m['role'] == 'user']
            assert [m.get('display_kind') for m in users] == [None, 'internal_notification'], users
            assert len(peer.requests) == 2
            persisted = db.get_messages_as_conversation('harso-native', include_row_ids=True)
            assert all(isinstance(m.get('_row_id'), int) for m in persisted if m['role'] in {'user', 'assistant'}), persisted

            assert 'LOCAL_ACK_' in json.dumps(db.get_messages('harso-native'))
            Path(os.environ['HERMES_HOME'], 'receipt.json').write_text(json.dumps({'rows': len(rows), 'requests': len(peer.requests), 'internal_typed': True, 'events': events}))
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    peer = ThreadingHTTPServer(('127.0.0.1', 0), ModelPeer)
    peer.requests, peer.metadata_requests = [], []
    peer.blocked, peer.release = threading.Event(), threading.Event()
    threading.Thread(target=peer.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{peer.server_port}/v1'
    os.environ.update(OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=base)
    os.environ.pop('HERMES_NATIVE_BOT_SESSIONS', None)
    Path(os.environ['HERMES_HOME'], 'config.yaml').write_text(
        f'gateway:\n  native_bot_sessions: true\nmodel:\n  default: local-wire-stub\n  provider: custom\n  base_url: {base}\n'
        f'auxiliary:\n  title_generation:\n    enabled: false\nterminal:\n  cwd: {os.environ["HERMES_HOME"]}\n')
    code = 0
    try:
        asyncio.run(probe(peer))
    except BaseException:
        traceback.print_exc()
        code = 1
    finally:
        peer.shutdown()
        peer.server_close()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
