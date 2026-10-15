import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from gateway.hub_revision import HubRevision, validate
from gateway import native_awake

REV = "a" * 64

@pytest.mark.parametrize("body", [{"server":"exa","revision":REV}, {"server":"weave-connectors","revision":"bad"}, {"server":"weave-connectors","revision":REV,"extra":1}, None])
def test_invalid_revision_selector(body):
    assert not validate(body)

@pytest.mark.asyncio
async def test_revision_refresh_nonblocking_single_flight_and_success_only(monkeypatch):
    held, unblock = asyncio.Event(), asyncio.Event()
    lease=[]
    async def acquire(ref): lease.append(ref); held.set(); await unblock.wait()
    monkeypatch.setattr(native_awake,"acquire",acquire)
    released=[];monkeypatch.setattr(native_awake,"release",released.append)
    adapter=SimpleNamespace(_mcp_reload_in_progress=False,gateway_runner=SimpleNamespace(_reload_mcp_toolset=AsyncMock(return_value={"schema_refreshed":"weave-connectors"})))
    revision=HubRevision(adapter)
    revision.request(REV);revision.request(REV)
    await asyncio.wait_for(held.wait(),3)
    assert revision.applied is None
    adapter.gateway_runner._reload_mcp_toolset.assert_not_called()
    assert len(lease)==1
    unblock.set();await revision.task
    assert revision.applied==REV and released==lease
    revision.request(REV)
    assert len(lease)==1

@pytest.mark.asyncio
async def test_failure_remains_pending_retry_and_new_revision_serializes(monkeypatch):
    monkeypatch.setattr(native_awake,"acquire",AsyncMock())
    monkeypatch.setattr(native_awake,"release",lambda ref: None)
    runner=SimpleNamespace(_reload_mcp_toolset=AsyncMock(side_effect=RuntimeError("list failed")))
    revision=HubRevision(SimpleNamespace(_mcp_reload_in_progress=False,gateway_runner=runner))
    revision.request(REV);await revision.task
    assert revision.applied is None and revision.desired==REV
    runner._reload_mcp_toolset.side_effect=None
    runner._reload_mcp_toolset.return_value={"schema_refreshed":"weave-connectors"}
    revision.request(REV);await revision.task
    assert revision.applied==REV and runner._reload_mcp_toolset.await_count==2

@pytest.mark.asyncio
async def test_helper_failure_never_runs_unheld_refresh(monkeypatch):
    monkeypatch.setattr(native_awake,"acquire",AsyncMock(side_effect=RuntimeError("down")))
    runner=SimpleNamespace(_reload_mcp_toolset=AsyncMock())
    revision=HubRevision(SimpleNamespace(_mcp_reload_in_progress=False,gateway_runner=runner))
    revision.request(REV);await revision.task
    assert revision.applied is None
    runner._reload_mcp_toolset.assert_not_called()


@pytest.mark.asyncio
async def test_changed_revision_waits_for_owning_refresh_and_never_overlaps(monkeypatch):
    monkeypatch.setattr(native_awake,"acquire",AsyncMock())
    monkeypatch.setattr(native_awake,"release",lambda ref:None)
    entered,release=asyncio.Event(),asyncio.Event()
    count=0;active=0;maximum=0
    async def refresh(**kwargs):
        nonlocal count,active,maximum
        count+=1;active+=1;maximum=max(active,maximum)
        if count==1:entered.set();await release.wait()
        active-=1
        return {"schema_refreshed":"weave-connectors"}
    adapter=SimpleNamespace(_mcp_reload_in_progress=False,gateway_runner=SimpleNamespace(_reload_mcp_toolset=refresh))
    revisions=HubRevision(adapter)
    revisions.request(REV);await entered.wait()
    revisions.request("b"*64)
    assert count==1 and revisions.applied is None
    release.set();await revisions.task
    assert revisions.applied=="b"*64 and count==2 and maximum==1


@pytest.fixture(autouse=True)
def _native_test_store_selection(monkeypatch):
    # v0.22 selects runtime-owned stores before explicit adapter overrides.
    from gateway.platforms import api_server_store
    original = api_server_store.selected_session_db
    def selected(adapter, home):
        return adapter._session_db if adapter._session_db is not None else original(adapter, home)
    monkeypatch.setattr(api_server_store, "selected_session_db", selected)
