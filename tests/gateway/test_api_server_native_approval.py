"""Weave: attended approvals on api_server native submit.

A native-submit turn has a live approval surface: the gateway approval queue
publishes ``approval.request`` on the submit stream, and the fork route
resolves it with once|deny only. These tests drive the real approval gate in
an agent thread against the real adapter handlers.
"""

import asyncio
import contextvars
import threading
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB
from tools import approval as ap


SESSION_ID = "native-approval-session"
SESSION_KEY = "agent:main:api_server:dm:native-approval-session"
REF = "native-approval-ref"
HEADERS = {"Authorization": "Bearer sk-native-approval"}


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    for key in ("HERMES_SINGLE_QUERY_SESSION", "HERMES_CRON_SESSION", "HERMES_YOLO_MODE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(ap, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(ap, "_get_approval_timeout", lambda: 60)
    config = {"mode": "manual"}
    monkeypatch.setattr(ap, "_get_approval_config", lambda: config)
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "sk-native-approval"}))
    db = SessionDB(tmp_path / "state.db")
    db.create_session(SESSION_ID, "api_server")
    db.register_native_session_submit(
        SESSION_ID, external_request_id="req-1", message_sha256="0" * 64,
        native_request_ref=REF,
    )
    adapter._session_db = db
    adapter._native_submit_ref_sessions[REF] = ("default", SESSION_ID)
    adapter.approval_config = config
    try:
        yield adapter
    finally:
        ap.mark_api_session_attended(SESSION_KEY, False)
        ap.clear_session(SESSION_KEY)
        db.close()


def _event():
    return type("Event", (), {"metadata": {"native_request_ref": REF}})()


async def _client(adapter):
    app = web.Application()
    app.router.add_get(
        "/api/sessions/{session_id}/submit/{native_request_ref}/approvals",
        adapter._handle_native_submit_approval_events,
    )
    app.router.add_post(
        "/api/sessions/{session_id}/submit/{native_request_ref}/approval/{request_id}",
        adapter._handle_native_submit_approval_response,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _start_gate(adapter, loop):
    """Run the shared approval gate on an agent thread bound like a native turn."""
    outcome: dict = {}

    def notify(data):
        asyncio.run_coroutine_threadsafe(
            adapter.send_exec_approval(
                chat_id=SESSION_ID, command=data["command"], session_key=SESSION_KEY,
                description=data["description"],
            ),
            loop,
        ).result(timeout=30)

    def run():
        APIServerAdapter._bind_api_server_session(chat_id=SESSION_ID, session_key=SESSION_KEY)
        token = ap.set_current_session_key(SESSION_KEY)
        ap.register_gateway_notify(SESSION_KEY, notify)
        try:
            outcome["result"] = ap._run_approval_gate(
                pattern_key="plugin_rule:ref-1", description="Create a Linear issue",
                display_target="linear.create_issue", cron_deny_message="cron",
                single_query_deny_message="single", autoapprove_log_prefix="test",
                fail_closed_when_no_human=True,
            )
        finally:
            ap.unregister_gateway_notify(SESSION_KEY)
            ap.reset_current_session_key(token)

    thread = threading.Thread(target=contextvars.Context().run, args=(run,), daemon=True)
    thread.start()
    return thread, outcome


async def _pending_request_id(adapter):
    # Loose deadline: the agent thread may be slow to start on a loaded runner.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        pending = ap.list_gateway_approvals(SESSION_KEY)
        if pending and adapter._native_submit_approvals_sent.get(REF):
            return pending[0]["request_id"]
        await asyncio.sleep(0.02)
    raise AssertionError("approval never became pending")


async def _stream_events(adapter):
    queue: asyncio.Queue = asyncio.Queue()
    adapter._native_submit_subscribers[REF] = (queue, asyncio.get_running_loop())
    return queue


@pytest.mark.asyncio
async def test_attended_turn_raises_card_and_once_runs_the_action(adapter):
    await adapter._on_native_submit_started(_event(), SESSION_KEY)
    stream = await _stream_events(adapter)
    thread, outcome = _start_gate(adapter, asyncio.get_running_loop())
    request_id = await _pending_request_id(adapter)

    card = stream.get_nowait()
    assert card["type"] == "approval.request"
    assert card["request_id"] == request_id
    assert card["description"] == "Create a Linear issue"
    assert card["choices"] == ["once", "deny"]

    client = await _client(adapter)
    try:
        response = await client.post(
            f"/api/sessions/{SESSION_ID}/submit/{REF}/approval/{request_id}",
            headers=HEADERS, json={"choice": "once"},
        )
        body = await response.json()
        replay = await client.post(
            f"/api/sessions/{SESSION_ID}/submit/{REF}/approval/{request_id}",
            headers=HEADERS, json={"choice": "once"},
        )
    finally:
        await client.close()
    await asyncio.to_thread(thread.join, 30)

    assert response.status == 200 and body["resolved"] is True
    assert replay.status == 409
    assert outcome["result"] == {"approved": True, "message": None}
    # once never persists: the same key still prompts next time.
    assert not ap.is_approved(SESSION_KEY, "plugin_rule:ref-1")


@pytest.mark.asyncio
async def test_deny_returns_the_native_blocked_text(adapter):
    await adapter._on_native_submit_started(_event(), SESSION_KEY)
    thread, outcome = _start_gate(adapter, asyncio.get_running_loop())
    request_id = await _pending_request_id(adapter)
    client = await _client(adapter)
    try:
        response = await client.post(
            f"/api/sessions/{SESSION_ID}/submit/{REF}/approval/{request_id}",
            headers=HEADERS, json={"choice": "deny"},
        )
    finally:
        await client.close()
    await asyncio.to_thread(thread.join, 30)

    assert response.status == 200
    assert outcome["result"]["approved"] is False
    assert outcome["result"]["message"].startswith("BLOCKED: Action denied by user.")
    assert "Do NOT retry" in outcome["result"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["always", "session", "approve", ""])
async def test_always_and_session_are_refused_and_nothing_is_persisted(adapter, choice, monkeypatch):
    saved = []
    monkeypatch.setattr(ap, "save_permanent_allowlist", lambda *a: saved.append(a))
    await adapter._on_native_submit_started(_event(), SESSION_KEY)
    thread, outcome = _start_gate(adapter, asyncio.get_running_loop())
    request_id = await _pending_request_id(adapter)
    client = await _client(adapter)
    try:
        refused = await client.post(
            f"/api/sessions/{SESSION_ID}/submit/{REF}/approval/{request_id}",
            headers=HEADERS, json={"choice": choice},
        )
        refused_body = await refused.json()
        still_pending = ap.list_gateway_approvals(SESSION_KEY)
        await client.post(
            f"/api/sessions/{SESSION_ID}/submit/{REF}/approval/{request_id}",
            headers=HEADERS, json={"choice": "deny"},
        )
    finally:
        await client.close()
    await asyncio.to_thread(thread.join, 30)

    assert refused.status == 400
    assert refused_body["error"]["code"] == "invalid_native_approval_choice"
    assert [a["request_id"] for a in still_pending] == [request_id]
    assert saved == []
    assert not ap.is_approved(SESSION_KEY, "plugin_rule:ref-1")


@pytest.mark.asyncio
async def test_reconnect_lists_and_replays_pending_approvals(adapter):
    await adapter._on_native_submit_started(_event(), SESSION_KEY)
    thread, _outcome = _start_gate(adapter, asyncio.get_running_loop())
    request_id = await _pending_request_id(adapter)
    client = await _client(adapter)
    try:
        listed = await client.get(f"/api/sessions/{SESSION_ID}/submit/{REF}/approvals", headers=HEADERS)
        listed_body = await listed.json()
        wrong_session = await client.get(f"/api/sessions/other/submit/{REF}/approvals", headers=HEADERS)
        await client.post(
            f"/api/sessions/{SESSION_ID}/submit/{REF}/approval/{request_id}",
            headers=HEADERS, json={"choice": "deny"},
        )
        cleared = await client.get(f"/api/sessions/{SESSION_ID}/submit/{REF}/approvals", headers=HEADERS)
        cleared_body = await cleared.json()
    finally:
        await client.close()
    await asyncio.to_thread(thread.join, 30)

    assert listed.status == 200
    assert [(e["type"], e["request_id"], e["choices"]) for e in listed_body["data"]] == [
        ("approval.request", request_id, ["once", "deny"]),
    ]
    assert wrong_session.status == 404
    assert cleared_body["data"] == []


@pytest.mark.asyncio
async def test_finished_turn_unmarks_the_session_and_denies_instantly_again(adapter):
    await adapter._on_native_submit_started(_event(), SESSION_KEY)
    assert SESSION_KEY in ap._attended_api_sessions
    await adapter._on_native_submit_finished(_event(), SESSION_KEY)
    assert SESSION_KEY not in ap._attended_api_sessions
    assert adapter._native_submit_approval_keys == {}

    thread, outcome = _start_gate(adapter, asyncio.get_running_loop())
    await asyncio.to_thread(thread.join, 30)
    assert outcome["result"]["approved"] is False
    assert "unattended platform (api_server)" in outcome["result"]["message"]


@pytest.mark.asyncio
async def test_config_off_keeps_native_turns_unattended(adapter):
    adapter.approval_config["native_submit_attended"] = False
    await adapter._on_native_submit_started(_event(), SESSION_KEY)
    assert SESSION_KEY not in ap._attended_api_sessions

    thread, outcome = _start_gate(adapter, asyncio.get_running_loop())
    await asyncio.to_thread(thread.join, 30)
    assert outcome["result"]["approved"] is False
    assert "unattended platform (api_server)" in outcome["result"]["message"]
    assert ap.list_gateway_approvals(SESSION_KEY) == []


@pytest.mark.asyncio
async def test_send_exec_approval_without_an_attended_turn_fails(adapter):
    result = await adapter.send_exec_approval(
        chat_id=SESSION_ID, command="rm -rf /tmp/x", session_key=SESSION_KEY,
        description="delete",
    )
    assert result.success is False
