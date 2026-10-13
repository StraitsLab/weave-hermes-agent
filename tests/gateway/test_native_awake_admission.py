import asyncio
import pytest
from unittest.mock import AsyncMock
from tests.gateway.test_api_server_session_submit import adapter, _client, _request, SESSION_ID
from gateway import native_awake
import pytest_asyncio

@pytest.fixture(autouse=True)
def auth(monkeypatch, adapter):
    adapter._expected_api_key = lambda: "sk-nat...test"


@pytest.mark.asyncio
async def test_admission_waits_for_hold_before_dispatch_and_replay_does_not_leak(adapter, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    calls=[]
    async def acquire(ref):
        calls.append(("acquire",ref)); entered.set(); await release.wait()
    monkeypatch.setattr(native_awake, "acquire", acquire)
    monkeypatch.setattr(native_awake, "release", lambda ref: calls.append(("release",ref)))
    async def admit(*args, **kwargs): calls.append(("dispatch",args[2])); return "streaming"
    adapter._admit_native_session_submit=admit
    client=await _client(adapter)
    try:
        post=asyncio.create_task(client.post(f"/api/sessions/{SESSION_ID}/submit",headers={"Authorization":"Bearer sk-nat...test"},json=_request()))
        await asyncio.wait_for(entered.wait(),3)
        assert [c[0] for c in calls] == ["acquire"] and not post.done()
        release.set(); response=await post
        assert response.status==202
        assert (await client.post(f"/api/sessions/{SESSION_ID}/submit",headers={"Authorization":"Bearer sk-nat...test"},json=_request())).status==202
        ref=(await response.json())["native_request_ref"]
        adapter._native_submit_close(ref,"turn.completed")
        assert [c[0] for c in calls] == ["acquire","dispatch","release"]
    finally: release.set(); await client.close()


@pytest.mark.asyncio
async def test_helper_down_refuses_before_dispatch_and_retry_recovers(adapter,monkeypatch):
    acquire=AsyncMock(side_effect=RuntimeError("helper down")); monkeypatch.setattr(native_awake,"acquire",acquire)
    adapter._admit_native_session_submit=AsyncMock(return_value="streaming")
    client=await _client(adapter)
    try:
        kw=dict(headers={"Authorization":"Bearer sk-nat...test"},json=_request())
        assert (await client.post(f"/api/sessions/{SESSION_ID}/submit",**kw)).status==503
        adapter._admit_native_session_submit.assert_not_called()
        acquire.side_effect=None
        assert (await client.post(f"/api/sessions/{SESSION_ID}/submit",**kw)).status==202
    finally: await client.close()


@pytest.mark.asyncio
async def test_receipt_failure_after_handoff_keeps_hold(adapter, monkeypatch):
    monkeypatch.setattr(native_awake,"acquire",AsyncMock())
    released=[]; monkeypatch.setattr(native_awake,"release",released.append)
    async def admit(*args,**kwargs): adapter._native_submit_handoffs[args[2]]="streaming"; return "streaming"
    adapter._admit_native_session_submit=admit
    monkeypatch.setattr(adapter._session_db,"set_native_session_submit_admission",lambda **kw: (_ for _ in ()).throw(RuntimeError("write failure")))
    client=await _client(adapter)
    try:
        assert (await client.post(f"/api/sessions/{SESSION_ID}/submit",headers={"Authorization":"Bearer sk-nat...test"},json=_request())).status==503
        assert not released
    finally: await client.close()


@pytest.mark.asyncio
async def test_submit_metadata_ack_and_auth_validation(adapter, monkeypatch):
    monkeypatch.setattr(native_awake,"acquire",AsyncMock())
    adapter._admit_native_session_submit=AsyncMock(return_value="streaming")
    from gateway.hub_revision import HubRevision
    revisions=[];monkeypatch.setattr(HubRevision,"request",lambda self, revision: revisions.append(revision))
    client=await _client(adapter)
    try:
        url=f"/api/sessions/{SESSION_ID}/submit";headers={"Authorization":"Bearer sk-nat...test"}
        body={**_request(),"hub_tools":{"server":"weave-connectors","revision":"a"*64}}
        assert (await client.post(url,json=body)).status==401
        assert not revisions
        response=await client.post(url,headers=headers,json=body)
        assert response.status==202 and (await response.json())["hub_tools"]==body["hub_tools"]
        assert revisions==["a"*64]
        body["hub_tools"]["server"]="exa"
        assert (await client.post(url,headers=headers,json=body)).status==400
    finally:await client.close()


@pytest.mark.asyncio
async def test_ambiguous_dispatch_retry_never_fakes_ack_or_releases(adapter,monkeypatch):
    monkeypatch.setattr(native_awake,"acquire",AsyncMock())
    released=[];monkeypatch.setattr(native_awake,"release",released.append)
    async def admit(*args,**kw):
        adapter._native_submit_handoffs[args[2]]="dispatch_pending"
        raise RuntimeError("RPC timeout")
    adapter._admit_native_session_submit=admit
    client=await _client(adapter)
    try:
        for _ in range(2):
            assert (await client.post(f"/api/sessions/{SESSION_ID}/submit",headers={"Authorization":"Bearer sk-nat...test"},json=_request())).status==503
        assert not released
    finally:await client.close()


@pytest.mark.asyncio
async def test_noncell_compatibility_and_required_helper_down(monkeypatch,tmp_path):
    monkeypatch.setattr(native_awake,"DIRECTORY",tmp_path/"missing")
    monkeypatch.setattr(native_awake,"SOCKET",tmp_path/"missing/hold.sock")
    monkeypatch.delenv("CELL_AWAKE_REQUIRED",raising=False)
    await native_awake.acquire("a"*32)
    monkeypatch.setenv("CELL_AWAKE_REQUIRED","1")
    with pytest.raises(OSError):await native_awake.acquire("a"*32)


async def direct_request(adapter):
    from types import SimpleNamespace
    adapter._check_auth = lambda _: None
    adapter._read_json_body = AsyncMock(return_value=(_request(), None))
    return await adapter._handle_session_submit(SimpleNamespace(match_info={"session_id": SESSION_ID}))


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["turn.completed", "turn.failed", "turn.started"])
async def test_pending_dispatch_evidence_replays_durably_without_new_hold(adapter, monkeypatch, event):
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    acquire = AsyncMock()
    monkeypatch.setattr(native_awake, "acquire", acquire)
    monkeypatch.setattr(native_awake, "release", lambda _: None)
    dispatched = []
    async def admit(*args, **kw):
        dispatched.append(args[2])
        adapter._native_submit_handoffs[args[2]] = "dispatch_pending"
        raise RuntimeError("lost acknowledgement")
    adapter._admit_native_session_submit = admit
    assert (await direct_request(adapter)).status == 503
    ref = dispatched[0]
    if event == "turn.started":
        adapter._native_submit_event(ref, event)
    else:
        adapter._native_submit_close(ref, event)
    response = await direct_request(adapter)
    assert response.status == 202
    import json
    assert json.loads(response.body)["native_request_ref"] == ref
    assert acquire.call_count == 1 and len(dispatched) == 1
    # New adapter, reopened actual SQLite DB: no in-memory evidence available.
    from hermes_state import SessionDB
    path = adapter._session_db.db_path
    adapter._session_db.close()
    db = SessionDB(path)
    adapter._session_db = db
    restarted = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test"}))
    restarted._session_db = db
    restarted.gateway_runner = adapter.gateway_runner
    restarted._admit_native_session_submit = AsyncMock(side_effect=AssertionError("no redispatch"))
    response = await direct_request(restarted)
    assert response.status == 202
    assert json.loads(response.body)["native_request_ref"] == ref
    assert acquire.call_count == 1


@pytest.mark.asyncio
async def test_cancel_before_handoff_releases_hold_and_registration(adapter, monkeypatch):
    acquire = AsyncMock()
    releases = []
    monkeypatch.setattr(native_awake, "acquire", acquire)
    monkeypatch.setattr(native_awake, "release", releases.append)
    entered = asyncio.Event()
    async def admit(*args, **kw):
        entered.set()
        await asyncio.Event().wait()
    adapter._admit_native_session_submit = admit
    task = asyncio.create_task(direct_request(adapter))
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert not adapter._native_awake_refs and len(releases) == 1
    assert adapter._session_db.native_submit_run("request-1") is None
    result = adapter._session_db.register_native_session_submit(SESSION_ID,
        external_request_id="request-1", message_sha256=adapter._native_submit_fingerprint("hello", "queue"),
        native_request_ref="new-ref")
    assert result["outcome"] == "inserted"


@pytest.mark.asyncio
async def test_cancel_acquire_waits_for_worker_then_releases_without_dispatch(adapter, monkeypatch):
    import threading
    entered, finish = threading.Event(), threading.Event()
    operations = []
    def request(op, ref):
        if op == "acquire":
            entered.set(); assert finish.wait(3)
        operations.append(op)
    monkeypatch.setattr(native_awake, "_request", request)
    adapter._admit_native_session_submit = AsyncMock(return_value="streaming")
    task = asyncio.create_task(direct_request(adapter))
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()  # cleanup must not race the acquiring worker
    finish.set()
    with pytest.raises(asyncio.CancelledError): await task
    for _ in range(30):
        if "release" in operations: break
        await asyncio.sleep(.01)
    assert operations == ["acquire", "release"]
    adapter._admit_native_session_submit.assert_not_called()
    assert not adapter._native_awake_refs


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["resume", "dispatch"])
async def test_cancel_native_rpc_does_not_dispatch_after_cleanup(adapter, monkeypatch, phase):
    from gateway import native_bot_admission as native
    from tui_gateway import server
    from types import SimpleNamespace
    import threading
    entered, finish = threading.Event(), threading.Event()
    calls, released = [], []
    monkeypatch.setattr(native, "enabled", lambda: True)
    monkeypatch.setattr(native_awake, "acquire", AsyncMock())
    monkeypatch.setattr(native_awake, "release", released.append)
    session = {"agent": SimpleNamespace(), "session_key": "key", "running": False}
    monkeypatch.setattr(server, "_sess", lambda *a: (session, None))
    def rpc(self, method, params):
        calls.append(method)
        if method == ("session.resume" if phase == "resume" else "prompt.submit"):
            entered.set(); assert finish.wait(3)
        return {"session_id": SESSION_ID, "status": "streaming"}
    monkeypatch.setattr(native.Projection, "rpc", rpc)
    task = asyncio.create_task(direct_request(adapter))
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError): await task
    await asyncio.sleep(.05)
    if phase == "resume":
        assert calls == ["session.resume"] and len(released) == 1
        assert not adapter._native_awake_refs
    else:
        assert calls == ["session.resume", "prompt.submit"] and not released
        assert (await direct_request(adapter)).status == 202
        ref = next(iter(adapter._native_awake_refs))
        adapter._native_submit_close(ref, "turn.completed")
        assert released == [ref]


@pytest.mark.asyncio
async def test_cancel_inflight_registration_removes_only_owned_reservation(adapter, monkeypatch):
    import threading
    entered, finish = threading.Event(), threading.Event()
    register = adapter._session_db.register_native_session_submit
    def delayed(*a, **kw):
        entered.set(); assert finish.wait(3)
        return register(*a, **kw)
    monkeypatch.setattr(adapter._session_db, "register_native_session_submit", delayed)
    acquire = AsyncMock()
    monkeypatch.setattr(native_awake, "acquire", acquire)
    task = asyncio.create_task(direct_request(adapter))
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel(); await asyncio.sleep(0); task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert register(SESSION_ID, external_request_id="request-1",
        message_sha256=adapter._native_submit_fingerprint("hello", "queue"),
        native_request_ref="next-ref")["outcome"] == "inserted"
    acquire.assert_not_called()
