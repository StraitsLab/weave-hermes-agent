"""DEC-415: on a call, the spoken reply to a pending clarify answers it through /submit.

Regression for the 19 Oct 2026 call: the caller's follow-ups queued behind a clarify the voice
could not read out, then ran as extra turns after hang-up.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import native_awake
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.platforms.api_server_harso_clarify import spoken_answer
from gateway.run_busy import GatewayBusySessionMixin
from hermes_state import SessionDB
from tools import approval, clarify_gateway
from tools.approval_gateway_wait import _ApprovalEntry
from tools.clarify_gateway import SKIPPED

SESSION, KEY, AUTH = "spoken-session", "spoken-key", {"Authorization": "Bearer spoken-test"}
CHOICES = ["Best route and destinations (Recommended)", "Business-class flights", "Luxury hotels and ryokans"]


@pytest.mark.parametrize("said, value", [
    ("Business-class flights", "Business-class flights"),
    ("business class flights.", "Business-class flights"),
    ("Let's do the business class flights", "Business-class flights"),
    ("2", "Business-class flights"),
    ("two", "Business-class flights"),
    ("the second one", "Business-class flights"),
    ("Best route and destinations", "Best route and destinations (Recommended)"),
    ("Skip.", SKIPPED),
    ("Something else entirely, research trains", "Something else entirely, research trains"),
    ("flights or hotels, whichever is quicker", "flights or hotels, whichever is quicker"),
])
def test_spoken_reply_names_a_choice_or_is_the_callers_own_answer(said, value):
    assert spoken_answer(said, CHOICES, multi_select=False) == value


def test_spoken_multi_select_names_every_choice_it_mentions():
    picked = spoken_answer("business class flights and luxury hotels and ryokans", CHOICES, multi_select=True)
    assert json.loads(picked) == ["Business-class flights", "Luxury hotels and ryokans"]


@pytest_asyncio.fixture
async def native(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session(SESSION, "api_server")
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "spoken-test"}))
    adapter._session_db = db
    queued = []
    adapter.gateway_runner = SimpleNamespace(
        _running=True, _draining=False, _startup_restore_in_progress=False,
        async_session_store=SimpleNamespace(bind_existing_session=AsyncMock(
            return_value=SimpleNamespace(session_key=KEY, session_id=SESSION))),
        session_credential_available=lambda *args: True,
        _is_session_running=lambda key: True,
        _enqueue_fifo=lambda key, event, owner: queued.append(event.text),
        _plaintext_approval_words=GatewayBusySessionMixin()._plaintext_approval_words,
    )
    adapter._active_sessions[KEY] = asyncio.Event()
    acquire, release = native_awake.acquire, native_awake.release
    native_awake.acquire, native_awake.release = AsyncMock(), lambda ref: None
    app = web.Application()
    app.router.add_post("/api/sessions/{session_id}/submit", adapter._handle_session_submit)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, queued
    finally:
        await client.close()
        native_awake.acquire, native_awake.release = acquire, release
        approval._gateway_queues.pop(KEY, None)
        clarify_gateway.clear_session(KEY)
        db.close()


def _handoff(request_id, message):
    return {"kind": "hermes.session.submit", "external_request_id": request_id, "message": message,
            "busy_mode": "steer", "internal": True, "hub_tools": {"server": "weave-connectors", "revision": "a" * 64}}


async def _send(client, request_id, message):
    response = await client.post(f"/api/sessions/{SESSION}/submit", headers=AUTH, json=_handoff(request_id, message))
    return response.status, await response.json()


@pytest.mark.asyncio
async def test_a_spoken_choice_answers_the_clarify_and_queues_nothing(native):
    client, queued = native
    first = clarify_gateway.register("c1", KEY, "Research first?", CHOICES)
    status, body = await _send(client, "h1", "Business class flights.")
    assert (status, body["admission"]) == (202, "answered_clarify")
    assert body["hub_tools"] == {"server": "weave-connectors", "revision": "a" * 64}  # weave-api requires the ack
    assert first.response == "Business-class flights" and queued == []

    # A retry of the same request after the next question opened replays; it never answers it.
    second = clarify_gateway.register("c2", KEY, "Arrive where?", ["Tokyo", "Osaka"])
    status, replay = await _send(client, "h1", "Business class flights.")
    assert (status, replay["admission"], replay["native_request_ref"]) == (202, "answered_clarify", body["native_request_ref"])
    assert not second.event.is_set() and queued == []


@pytest.mark.asyncio
async def test_a_retried_yes_never_approves_the_next_action(native):
    client, queued = native
    approval._gateway_queues[KEY] = [first := _ApprovalEntry({"request_id": "a"})]
    status, body = await _send(client, "y1", "yes")
    assert (status, body["admission"], first.result) == (202, "answered_approval", "once")
    approval._gateway_queues[KEY] = [second := _ApprovalEntry({"request_id": "b"})]
    status, replay = await _send(client, "y1", "yes")
    assert (status, replay["admission"]) == (202, "answered_approval")
    assert second.result is None and queued == []


@pytest.mark.asyncio
async def test_with_nothing_pending_a_handoff_still_queues_its_own_turn(native):
    client, queued = native
    status, body = await _send(client, "w1", "Work result: done")
    assert (status, body["admission"], queued) == (202, "queued", ["Work result: done"])


@pytest.mark.asyncio
async def test_skills_endpoint_reads_the_real_skill_catalog(tmp_path):
    """Regression: /v1/skills passed an argument _find_all_skills does not take, so every call
    was a 500 and the desktop kept a permanent workspace-setup banner."""
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "spoken-test"}))
    app = web.Application()
    app.router.add_get("/v1/skills", adapter._handle_skills)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/v1/skills", headers=AUTH)
        assert response.status == 200
        assert (await response.json())["object"] == "list"
