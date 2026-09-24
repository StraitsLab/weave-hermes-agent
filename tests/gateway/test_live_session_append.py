"""Behavior tests for the native authenticated passive session append."""

import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB, SessionPassiveAppendError


SESSION_ID = "018f22e2-7c00-7001-8001-000000000010"
OTHER_SESSION_ID = "018f22e2-7c00-7001-8001-000000000011"
HERMES_SESSION_ID = "weave-018f22e2-7c00-7001-8001-000000000099"
ITEM_ONE = "018f22e2-7c00-7001-8001-000000000001"
ITEM_TWO = "018f22e2-7c00-7001-8001-000000000002"
PARTICIPANT_ID = "018f22e2-7c00-7001-8001-000000000003"


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _request(item_id=ITEM_ONE, *, target=SESSION_ID, role="user", content="hello", predecessor=None):
    request = {
        "kind": "hermes_append_request",
        "authenticated": True,
        "external_item_id": item_id,
        "external_identity_scope": "global",
        "transcript_row": "normal",
        "atomic_insert_or_return": True,
        "target_bem_session_id": target,
        "role": role,
        "canonical_sha256": _digest(content),
        "participant_id": PARTICIPANT_ID,
    }
    if predecessor is not None:
        request["predecessor_sequence"] = predecessor
    return {"request": request, "content": content}


@pytest.fixture
def session_db(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session(SESSION_ID, "api_server")
    try:
        yield db
    finally:
        db.close()


def _append(
    db,
    item_id=ITEM_ONE,
    *,
    session_id=SESSION_ID,
    target_bem_session_id=SESSION_ID,
    role="user",
    content="hello",
    predecessor=None,
):
    return db.append_passive_message(
        session_id,
        target_bem_session_id=target_bem_session_id,
        external_item_id=item_id,
        role=role,
        content=content,
        canonical_sha256=_digest(content),
        participant_id=PARTICIPANT_ID,
        predecessor_sequence=predecessor,
    )


def test_native_insert_replay_conflict_and_order(session_db):
    inserted = _append(session_db)
    assert inserted["outcome"] == "inserted"
    assert inserted["sequence"] == 1

    replay = _append(session_db)
    assert replay == {
        "outcome": "identical_retry",
        "message_id": inserted["message_id"],
        "sequence": 1,
    }
    assert _append(session_db, content="changed")["outcome"] == "idempotency_conflict"

    second = _append(
        session_db,
        ITEM_TWO,
        role="assistant",
        content="answer",
        predecessor=1,
    )
    assert second["outcome"] == "inserted"
    messages = session_db.get_messages(SESSION_ID)
    assert [row["content"] for row in messages] == ["hello", "answer"]
    assert messages[0]["platform_message_id"] is None
    assert messages[0]["display_metadata"]["participant_id"] == PARTICIPANT_ID
    assert messages[0]["display_kind"] == "passive_append"
    conversation = session_db.get_messages_as_conversation(SESSION_ID)
    assert "display_metadata" not in conversation[0]
    assert "display_kind" not in conversation[0]


def test_identical_replay_ignores_advanced_predecessor_cursor(session_db):
    session_db.append_message(SESSION_ID, "user", "prior")
    inserted = _append(
        session_db,
        ITEM_TWO,
        role="assistant",
        content="answer",
        predecessor=1,
    )
    assert inserted["outcome"] == "inserted"
    assert inserted["sequence"] == 2

    replay = _append(
        session_db,
        ITEM_TWO,
        role="assistant",
        content="answer",
        predecessor=2,
    )
    assert replay == {
        "outcome": "identical_retry",
        "message_id": inserted["message_id"],
        "sequence": 2,
    }
    changed = _append(
        session_db,
        ITEM_TWO,
        role="assistant",
        content="changed",
        predecessor=2,
    )
    assert changed["outcome"] == "idempotency_conflict"
    assert session_db.message_count(SESSION_ID) == 2


def test_idempotency_survives_message_and_session_deletion(session_db):
    inserted = _append(session_db)
    session_db.replace_messages(SESSION_ID, [])
    assert session_db.message_count(SESSION_ID) == 0
    with session_db._read_ctx() as conn:
        assert not conn.execute(
            "PRAGMA foreign_key_list('passive_append_idempotency')"
        ).fetchall()

    replay = _append(session_db)
    assert replay == {
        "outcome": "identical_retry",
        "message_id": inserted["message_id"],
        "sequence": 1,
    }
    assert session_db.message_count(SESSION_ID) == 0
    assert _append(session_db, content="changed")["outcome"] == "idempotency_conflict"

    session_db.create_session(OTHER_SESSION_ID, "api_server")
    assert _append(session_db, session_id=OTHER_SESSION_ID,
        target_bem_session_id=OTHER_SESSION_ID)["outcome"] == "idempotency_conflict"
    assert session_db.delete_session(SESSION_ID)
    assert _append(session_db)["outcome"] == "identical_retry"


def test_legacy_mapping_recovers_derivable_bem_identity(session_db):
    def seed_legacy(conn):
        conn.execute(
            """CREATE TABLE passive_append_idempotency (
               external_item_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
               native_message_id INTEGER NOT NULL, canonical_sha256 TEXT NOT NULL,
               role TEXT NOT NULL, participant_id TEXT NOT NULL,
               predecessor_sequence INTEGER)"""
        )
        conn.execute(
            "INSERT INTO passive_append_idempotency VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ITEM_ONE, SESSION_ID, 77, _digest("hello"), "user", PARTICIPANT_ID, None),
        )

    session_db._execute_write(seed_legacy)
    assert _append(session_db) == {
        "outcome": "identical_retry", "message_id": 77, "sequence": 1,
    }
    assert _append(session_db,
        target_bem_session_id=OTHER_SESSION_ID)["outcome"] == "idempotency_conflict"
    with session_db._read_ctx() as conn:
        row = conn.execute(
            "SELECT target_bem_session_id FROM passive_append_idempotency"
        ).fetchone()
    assert row["target_bem_session_id"] == SESSION_ID


def test_legacy_opaque_native_ref_fails_closed(session_db):
    def seed_legacy(conn):
        conn.execute(
            """CREATE TABLE passive_append_idempotency (
               external_item_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
               native_message_id INTEGER NOT NULL, canonical_sha256 TEXT NOT NULL,
               role TEXT NOT NULL, participant_id TEXT NOT NULL,
               predecessor_sequence INTEGER)"""
        )
        conn.execute(
            "INSERT INTO passive_append_idempotency VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ITEM_ONE, HERMES_SESSION_ID, 77, _digest("hello"), "user", PARTICIPANT_ID, None),
        )

    session_db._execute_write(seed_legacy)
    assert _append(session_db)["outcome"] == "idempotency_conflict"


def test_passive_metadata_is_native_display_only(session_db):
    _append(session_db)
    model_history, display_history = session_db.get_resume_conversations(SESSION_ID)
    assert "display_metadata" not in model_history[0]
    assert "display_kind" not in model_history[0]
    assert display_history[0]["display_metadata"]["participant_id"] == PARTICIPANT_ID
    assert display_history[0]["display_kind"] == "passive_append"


def test_predecessor_gap_roles_and_digest_fail_closed(session_db):
    assert _append(session_db, predecessor=1)["outcome"] == "sequence_gap"
    with pytest.raises(SessionPassiveAppendError) as role_error:
        _append(session_db, role="system")
    assert role_error.value.code == "invalid_role"
    with pytest.raises(SessionPassiveAppendError) as non_string_role_error:
        _append(session_db, role=[])
    assert non_string_role_error.value.code == "invalid_role"
    with pytest.raises(SessionPassiveAppendError) as participant_error:
        session_db.append_passive_message(
            SESSION_ID,
            target_bem_session_id=SESSION_ID,
            external_item_id=ITEM_ONE,
            role="user",
            content="hello",
            canonical_sha256=_digest("hello"),
            participant_id="not-a-uuid",
        )
    assert participant_error.value.code == "invalid_participant_id"
    with pytest.raises(SessionPassiveAppendError) as digest_error:
        session_db.append_passive_message(
            SESSION_ID,
            target_bem_session_id=SESSION_ID,
            external_item_id=ITEM_ONE,
            role="user",
            content="hello",
            canonical_sha256="0" * 64,
            participant_id=PARTICIPANT_ID,
        )
    assert digest_error.value.code == "invalid_canonical_digest"


def test_passive_identity_is_global_but_native_platform_ids_coexist(session_db):
    session_db.create_session(OTHER_SESSION_ID, "api_server")
    session_db.append_message(
        SESSION_ID,
        "user",
        "ordinary platform message",
        platform_message_id=ITEM_ONE,
    )
    passive = _append(session_db, predecessor=1, role="assistant", content="passive")
    assert passive["outcome"] == "inserted"
    messages = session_db.get_messages(SESSION_ID)
    assert messages[0]["platform_message_id"] == ITEM_ONE
    assert messages[1]["platform_message_id"] is None

    collision = session_db.append_passive_message(
        OTHER_SESSION_ID,
        target_bem_session_id=OTHER_SESSION_ID,
        external_item_id=ITEM_ONE,
        role="user",
        content="passive",
        canonical_sha256=_digest("passive"),
        participant_id=PARTICIPANT_ID,
    )
    assert collision["outcome"] == "idempotency_conflict"


def test_concurrent_duplicate_has_one_mapping_and_native_row(session_db):
    peers = [SessionDB(session_db.db_path), SessionDB(session_db.db_path)]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda db: _append(db), peers))
        assert sorted(result["outcome"] for result in results) == [
            "identical_retry",
            "inserted",
        ]
        assert session_db.message_count(SESSION_ID) == 1
        with session_db._read_ctx() as conn:
            assert conn.execute("SELECT COUNT(*) FROM passive_append_idempotency").fetchone()[0] == 1
    finally:
        for peer in peers:
            peer.close()


def _app(adapter):
    app = web.Application()
    app.router.add_post(
        "/api/sessions/{session_id}/append", adapter._handle_session_append
    )
    return app


@pytest.mark.asyncio
async def test_http_exact_wrapper_constants_target_and_direct_receipt(session_db):
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "sk-passive-append-test"})
    )
    adapter._session_db = session_db
    session_db.create_session(HERMES_SESSION_ID, "api_server")
    headers = {"Authorization": "Bearer sk-passive-append-test"}
    app = _app(adapter)
    async with TestClient(TestServer(app)) as client:
        unauthorized = await client.post(
            f"/api/sessions/{SESSION_ID}/append", json=_request()
        )
        assert unauthorized.status == 401

        first = await client.post(
            f"/api/sessions/{HERMES_SESSION_ID}/append", headers=headers, json=_request()
        )
        assert first.status == 201
        receipt = await first.json()
        assert set(receipt) == {
            "kind", "receipt_id", "external_item_id", "outcome", "native_item_ref",
            "terminal", "retryable", "ledger_recording", "cross_service_2pc",
        }
        assert receipt["kind"] == "hermes_append_receipt"
        assert receipt["outcome"] == "inserted"
        native_ref = receipt["native_item_ref"]

        replay = await client.post(
            f"/api/sessions/{HERMES_SESSION_ID}/append", headers=headers, json=_request()
        )
        assert replay.status == 200
        replay_receipt = await replay.json()
        assert replay_receipt["outcome"] == "identical_retry"
        assert replay_receipt["native_item_ref"] == native_ref
        assert replay_receipt["same_native_ref_on_identical_retry"] is True

        changed_bem = await client.post(
            f"/api/sessions/{HERMES_SESSION_ID}/append", headers=headers,
            json=_request(target=OTHER_SESSION_ID),
        )
        assert changed_bem.status == 409
        assert (await changed_bem.json())["outcome"] == "idempotency_conflict"

        assert session_db.delete_session(HERMES_SESSION_ID)
        assert session_db.message_count(HERMES_SESSION_ID) == 0
        tombstone_replay = await client.post(
            f"/api/sessions/{HERMES_SESSION_ID}/append", headers=headers, json=_request()
        )
        assert tombstone_replay.status == 200
        tombstone_receipt = await tombstone_replay.json()
        assert tombstone_receipt["outcome"] == "identical_retry"
        assert tombstone_receipt["native_item_ref"] == native_ref
        assert session_db.message_count(HERMES_SESSION_ID) == 0

        changed = await client.post(
            f"/api/sessions/{HERMES_SESSION_ID}/append",
            headers=headers,
            json=_request(content="changed"),
        )
        assert changed.status == 409
        assert (await changed.json())["outcome"] == "idempotency_conflict"

        unknown = await client.post(
            f"/api/sessions/{HERMES_SESSION_ID}/append",
            headers=headers,
            json=_request(item_id=ITEM_TWO),
        )
        assert unknown.status == 404
        assert (await unknown.json())["error"]["code"] == "session_not_found"

        invalid_target = await client.post(
            f"/api/sessions/{HERMES_SESSION_ID}/append",
            headers=headers,
            json=_request(target="not-a-uuid"),
        )
        assert invalid_target.status == 400
        assert (await invalid_target.json())["error"]["code"] == "invalid_append_schema"

        extra = _request()
        extra["request"]["unexpected"] = True
        rejected = await client.post(
            f"/api/sessions/{SESSION_ID}/append", headers=headers, json=extra
        )
        assert rejected.status == 400


@pytest.mark.asyncio
async def test_http_required_constants_participant_gap_conflict_and_no_inference(session_db):
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "sk-passive-append-test"})
    )
    adapter._session_db = session_db
    adapter._run_agent = lambda **_: (_ for _ in ()).throw(AssertionError("inference called"))
    headers = {"Authorization": "Bearer sk-passive-append-test"}
    app = _app(adapter)
    async with TestClient(TestServer(app)) as client:
        malformed = _request(predecessor=1)
        malformed["request"]["authenticated"] = False
        rejected = await client.post(
            f"/api/sessions/{SESSION_ID}/append", headers=headers, json=malformed
        )
        assert rejected.status == 400

        non_string_role = _request()
        non_string_role["request"]["role"] = []
        rejected_role = await client.post(
            f"/api/sessions/{SESSION_ID}/append",
            headers=headers,
            json=non_string_role,
        )
        assert rejected_role.status == 400

        missing_participant = _request()
        del missing_participant["request"]["participant_id"]
        rejected_participant = await client.post(
            f"/api/sessions/{SESSION_ID}/append",
            headers=headers,
            json=missing_participant,
        )
        assert rejected_participant.status == 400

        gap = await client.post(
            f"/api/sessions/{SESSION_ID}/append",
            headers=headers,
            json=_request(predecessor=1),
        )
        assert gap.status == 409
        assert (await gap.json())["outcome"] == "sequence_gap"

        inserted = await client.post(
            f"/api/sessions/{SESSION_ID}/append", headers=headers, json=_request()
        )
        assert inserted.status == 201

        conflict_body = _request(content="different")
        conflict = await client.post(
            f"/api/sessions/{SESSION_ID}/append",
            headers=headers,
            json=conflict_body,
        )
        assert conflict.status == 409
        assert (await conflict.json())["outcome"] == "idempotency_conflict"


# --- Weave: multi-row passive append group (card + answer stay adjacent) ------

CARD_ID = "018f22e2-7c00-7001-8001-000000000201"
ANSWER_ID = "018f22e2-7c00-7001-8001-000000000202"
OTHER_ID = "018f22e2-7c00-7001-8001-000000000203"


def _item(item_id, role, content):
    return {
        "target_bem_session_id": SESSION_ID,
        "external_item_id": item_id,
        "role": role,
        "content": content,
        "canonical_sha256": _digest(content),
        "participant_id": PARTICIPANT_ID,
    }


def _pair():
    return [_item(CARD_ID, "user", "RESULT_CARD"), _item(ANSWER_ID, "assistant", "RESULT_MESSAGE")]


def _rows(db):
    return [(m["role"], m["content"]) for m in db.get_messages(SESSION_ID)]


def test_group_inserts_contiguously_replays_and_refuses_mixes(session_db):
    first = session_db.append_passive_messages(SESSION_ID, _pair())
    assert [r["outcome"] for r in first] == ["inserted", "inserted"]
    assert [r["sequence"] for r in first] == [1, 2]

    # Crash-after-commit replay with a stale predecessor still answers identically.
    replay = session_db.append_passive_messages(SESSION_ID, _pair(), predecessor_sequence=1)
    assert [r["outcome"] for r in replay] == ["identical_retry", "identical_retry"]
    assert [r["message_id"] for r in replay] == [r["message_id"] for r in first]

    mixed = session_db.append_passive_messages(
        SESSION_ID, [_item(CARD_ID, "user", "RESULT_CARD"), _item(OTHER_ID, "assistant", "X")],
        predecessor_sequence=2,
    )
    assert [r["outcome"] for r in mixed] == ["idempotency_conflict", "idempotency_conflict"]
    assert _rows(session_db) == [("user", "RESULT_CARD"), ("assistant", "RESULT_MESSAGE")]


def test_group_stale_tail_writes_nothing(session_db):
    session_db.append_message(SESSION_ID, "user", "EARLIER")
    gap = session_db.append_passive_messages(SESSION_ID, _pair(), predecessor_sequence=None)
    assert [r["outcome"] for r in gap] == ["sequence_gap", "sequence_gap"]
    assert gap[0]["sequence"] == 1
    assert _rows(session_db) == [("user", "EARLIER")]
    placed = session_db.append_passive_messages(SESSION_ID, _pair(), predecessor_sequence=1)
    assert [r["sequence"] for r in placed] == [2, 3]


def test_group_rejects_bad_shapes_before_writing(session_db):
    with pytest.raises(SessionPassiveAppendError):
        session_db.append_passive_messages(SESSION_ID, [])
    with pytest.raises(SessionPassiveAppendError):
        session_db.append_passive_messages(SESSION_ID, [_item(CARD_ID, "user", "a")] * 2)
    with pytest.raises(SessionPassiveAppendError):
        session_db.append_passive_messages(SESSION_ID, [_item(CARD_ID, "user", "a")] * 9)
    bad = _item(ANSWER_ID, "assistant", "b")
    bad["canonical_sha256"] = _digest("other")
    with pytest.raises(SessionPassiveAppendError):
        session_db.append_passive_messages(SESSION_ID, [_item(CARD_ID, "user", "a"), bad])
    assert _rows(session_db) == []


def test_group_stays_adjacent_under_a_concurrent_writer(session_db):
    """Scratch group probe as a test: ordinary writers never split the pair."""
    import threading

    session_db.append_message(SESSION_ID, "user", "EARLIER")
    stop = threading.Event()

    def writer():
        n = 0
        while not stop.is_set():
            session_db.append_message(SESSION_ID, "user", f"CONCURRENT_{n}")
            n += 1

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        for _attempt in range(500):
            tail = session_db.get_session(SESSION_ID)["message_count"] or None
            out = session_db.append_passive_messages(SESSION_ID, _pair(), predecessor_sequence=tail)
            assert len({r["outcome"] for r in out}) == 1  # never a partial group
            if out[0]["outcome"] == "inserted":
                break
    finally:
        stop.set()
        thread.join()
    assert [r["outcome"] for r in out] == ["inserted", "inserted"]
    contents = [content for _role, content in _rows(session_db)]
    card_at = contents.index("RESULT_CARD")
    assert contents[card_at + 1] == "RESULT_MESSAGE"
    assert contents.count("RESULT_CARD") == contents.count("RESULT_MESSAGE") == 1
    # Single-row API unchanged alongside the group.
    single = _append(session_db, ITEM_ONE, predecessor=session_db.get_session(SESSION_ID)["message_count"])
    assert single["outcome"] == "inserted"
    assert _append(session_db, ITEM_ONE)["outcome"] == "identical_retry"


def _group_body(requests, predecessor=None):
    body = {
        "requests": [r["request"] for r in requests],
        "contents": [r["content"] for r in requests],
    }
    if predecessor is not None:
        body["predecessor_sequence"] = predecessor
    return body


@pytest.mark.asyncio
async def test_http_group_receipts_status_and_schema(session_db):
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "sk-group"}))
    adapter._session_db = session_db
    headers = {"Authorization": "Bearer sk-group"}
    app = web.Application()
    app.router.add_post(
        "/api/sessions/{session_id}/append/group", adapter._handle_session_append_group
    )
    pair = [
        _request(CARD_ID, role="user", content="RESULT_CARD"),
        _request(ANSWER_ID, role="assistant", content="RESULT_MESSAGE"),
    ]
    url = f"/api/sessions/{SESSION_ID}/append/group"
    async with TestClient(TestServer(app)) as client:
        assert (await client.post(url, json=_group_body(pair))).status == 401

        first = await client.post(url, headers=headers, json=_group_body(pair))
        assert first.status == 201
        receipt = await first.json()
        assert receipt["kind"] == "hermes_append_group_receipt"
        assert [r["outcome"] for r in receipt["receipts"]] == ["inserted", "inserted"]
        assert [r["external_item_id"] for r in receipt["receipts"]] == [CARD_ID, ANSWER_ID]

        replay = await client.post(url, headers=headers, json=_group_body(pair, predecessor=1))
        assert replay.status == 200
        replay_receipts = (await replay.json())["receipts"]
        assert [r["native_item_ref"] for r in replay_receipts] == [
            r["native_item_ref"] for r in receipt["receipts"]
        ]

        gap = await client.post(
            url, headers=headers,
            json=_group_body([_request(OTHER_ID, content="late")], predecessor=1),
        )
        assert gap.status == 409
        assert (await gap.json())["receipts"][0]["retryable"] is True

        per_row_predecessor = _group_body(pair)
        per_row_predecessor["requests"][0]["predecessor_sequence"] = 2
        assert (await client.post(url, headers=headers, json=per_row_predecessor)).status == 400

        bad_digest = _group_body([_request(OTHER_ID, content="x")])
        bad_digest["contents"] = ["tampered"]
        rejected = await client.post(url, headers=headers, json=bad_digest)
        assert rejected.status == 400
        assert (await rejected.json())["error"]["code"] == "invalid_canonical_digest"

        too_many = _group_body([_request(OTHER_ID)] * 9)
        assert (await client.post(url, headers=headers, json=too_many)).status == 400

        missing = await client.post(
            "/api/sessions/no-such-session/append/group", headers=headers,
            json=_group_body([_request(OTHER_ID, content="late")]),
        )
        assert missing.status == 404
    assert [c for _r, c in _rows(session_db)] == ["RESULT_CARD", "RESULT_MESSAGE"]
