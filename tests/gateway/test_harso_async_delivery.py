"""Native Harso consumers use the canonical producer FIFO, never a user self-post."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.run import GatewayRunner
from gateway.session_authority import SessionAuthority, initialize_session_authority
from gateway.session_contract import SessionRef
from gateway.session_envelope import restore_native
from hermes_state import SessionDB
from hermes_state_runtime import list_session_admissions
from tests.gateway.test_api_server_session_submit import (
    internal_gateway as _internal_gateway, _bind_internal_gateway, _submit_internal_gateway, _wait_native_terminal, SESSION_ID,
)

internal_gateway = _internal_gateway


@pytest_asyncio.fixture
async def native(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr('gateway.weave_chat_sync.publish', lambda *a, **kw: False)
    monkeypatch.setattr(SessionAuthority, '_schedule', lambda self, ref: None)
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    db = SessionDB(tmp_path / 'state.db')
    runner.session_store._db = db
    runner._session_db = db
    runner._running = True
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter.gateway_runner = runner
    runner.adapters = {Platform.API_SERVER: adapter}
    runner._wire_adapter_handlers(adapter)
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='async-test', db=db)
    db.create_session('weave-chat', source='api_server')
    adapter._ensure_session_db_async = AsyncMock(return_value=db)
    adapter._ensure_session_db = lambda: db
    adapter._wait_native_submit_start = AsyncMock(return_value='streaming')
    adapter.handle_message = AsyncMock()
    monkeypatch.setattr('gateway.platforms.api_server_native_model_lock.apply_native_model_lock', AsyncMock())
    yield SimpleNamespace(runner=runner, db=db, adapter=adapter, authority=authority)
    db.close()


@pytest.mark.asyncio
async def test_native_submit_binds_detached_delegate_and_terminal_capability(native, monkeypatch):
    n = native
    await n.adapter._admit_native_session_submit('weave-chat', 'hello', 'human')
    import asyncio
    await asyncio.sleep(0)
    event = n.adapter.handle_message.call_args.args[0]
    from gateway.native_session_lifecycle import run
    from gateway.session_context import session_history_delivery_supported
    from tools.delegate_tool_dispatch import _resolve_async_wake_sid
    from tools.terminal_tool_background import _apply_async_support
    observed = []
    async def execute(*args):
        from tests.tools.test_delegate_apiserver_background import _patch_delegate, _fake_parent
        import json
        context = SimpleNamespace(source=event.source, session_id='weave-chat', session_key=event.metadata['gateway_session_key'])
        tokens = n.runner._set_session_env(context)
        try:
            proc = SimpleNamespace(id='proc')
            observed.append((session_history_delivery_supported(), _resolve_async_wake_sid('weave-chat', True),
                             _apply_async_support(proc, {}, True, None)))
            assert proc.parent_session_id == 'weave-chat'
            dt = _patch_delegate(monkeypatch)
            handle = json.loads(dt.delegate_task(goal='test helper', background=True, parent_agent=_fake_parent()))
            assert handle['mode'] == 'background' and handle['status'] == 'dispatched'
            from tests.tools.test_delegate_apiserver_background import _drain_one
            assert _drain_one()['type'] == 'async_delegation'
        finally:
            n.runner._clear_session_env(tokens)
    n.runner._handle_message_with_agent = execute
    n.adapter._on_native_submit_started = AsyncMock()
    n.adapter._on_native_submit_finished = AsyncMock()
    await run(n.runner, event, event.source, event.metadata['gateway_session_key'], 1)
    assert observed == [(True, '', (True, None))]


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['async_delegation', 'completion'])
async def test_completion_admitted_once_behind_active_turn_and_restored_internal(native, kind, monkeypatch):
    n = native
    await n.adapter._admit_native_session_submit('weave-chat', 'hello', 'human')
    import asyncio
    await asyncio.sleep(0)
    event = n.adapter.handle_message.call_args.args[0]
    key = event.metadata['gateway_session_key']
    n.authority._classic_turn_holds = lambda route: True
    n.adapter._native_submit_answers_approval = lambda *a: pytest.fail('completion answered approval')
    monkeypatch.setattr('gateway.platforms.api_server_harso_clarify.pending_clarify_answer',
                        lambda *a: pytest.fail('completion answered clarify'))
    evt = dict(type=kind, session_id='proc', started_at=123, delegation_id='delegation', session_key=key)
    assert await n.runner._self_post_api_server(n.adapter, 'yes', 'weave-chat', evt)
    assert await n.runner._self_post_api_server(n.adapter, 'yes', 'weave-chat', evt)
    rows = list_session_admissions(n.db, session_id='weave-chat', pending_only=False)
    assert len(rows) == 1
    row = rows[0]
    assert row['status'] == 'queued'
    ref = SessionRef(n.authority.profile_id, 'weave-chat')
    assert (await n.authority._claim_next(ref, n.authority.sessions['weave-chat']))[0] == 'classic_busy'
    restored = restore_native(row['payload'], n.runner)
    assert restored.internal and restored.text == 'yes'
    assert restored.metadata['gateway_session_id'] == 'weave-chat'
    # A restored API source must retain its shared-listener transport, including
    # when the profile itself has no API adapter.
    from gateway.session_api import _pin_api_identity
    from unittest.mock import Mock
    pin = Mock(wraps=_pin_api_identity)
    monkeypatch.setattr('gateway.session_api._pin_api_identity', pin)
    restored = restore_native(row['payload'], n.runner)
    assert pin.called
    # The native lifecycle projects the follow-up through the SAME chat writer.
    seen = []
    async def handle(e):
        seen.append(e)
        return 'follow-up'
    n.runner._handle_message = handle
    from gateway.session_ingress import execute_admission
    await execute_admission(n.authority, ref, {**row, 'generation': 1})
    assert seen[0].internal
    assert seen[0].metadata['native_request_ref'] == row['admission_id']
    assert 'api_turn_v1' not in row['payload']
    # Simulate lost in-memory bindings; boot recovery restores and schedules the durable head.
    n.authority.sessions.clear()
    scheduled = []
    n.authority._schedule = scheduled.append
    from gateway.session_api_automation import recover
    recover(n.adapter, n.authority)
    assert scheduled == [ref]


@pytest.mark.asyncio
async def test_unmarked_api_session_keeps_delivery_only(native, monkeypatch):
    from gateway.session_api import bind_api_session
    n = native
    bind_api_session(n.authority, 'plain-api')
    persist = AsyncMock()
    monkeypatch.setattr('gateway.wake.persist_delegation_delivery', persist)
    assert await n.runner._self_post_api_server(n.adapter, 'result', 'plain-api', {'type': 'async_delegation'})
    persist.assert_awaited_once()
    assert list_session_admissions(n.db, session_id='plain-api', pending_only=False) == []


@pytest.mark.asyncio
async def test_completion_registers_crash_cleanup_and_runtime_lock(native):
    n = native
    await n.adapter._admit_native_session_submit('weave-chat', 'hello', 'human')
    n.db.update_session_runtime_lock('weave-chat', model='locked-model', provider='custom', confirmed=True)
    await n.runner._self_post_api_server(n.adapter, 'done', 'weave-chat',
                                       dict(type='async_delegation', delegation_id='crash'))
    row = next(r for r in list_session_admissions(n.db, session_id='weave-chat', pending_only=False)
               if r['principal_id'].startswith('automation:'))
    from gateway.session_api_automation import prepare_event
    from gateway.session_envelope import restore_native
    from gateway.session_api_turn import api_execution, api_runtime_locked
    token = api_execution.set(None)
    try:
        await prepare_event(n.authority, SessionRef('default', 'weave-chat'), row,
                            restore_native(row['payload'], n.runner))
        assert api_runtime_locked()['settings']['requested_model'] == 'locked-model'
        assert any(r['native_request_ref'] == row['admission_id'] for r in n.db.pending_native_session_submits())
        from gateway.run_turn_runner import _settle_api_runtime_lock
        n.adapter._turn_runtime_metadata = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('runtime mismatch'))
        result = {'final_response': 'wrong-model answer'}
        _settle_api_runtime_lock(SimpleNamespace(), result, {'usage': {}})
        assert result['failed'] and result['final_response'] == ''
        # Existing boot terminal cleanup can now find the automation ref.
        from hermes_state_runtime import claim_session_input, settle_session_input
        claim_session_input(n.db, epoch=n.authority.epoch, session_id='weave-chat')
        n.runner._native_submit_unclean_boot = True
        n.runner.config.multiplex_profiles = False
        # The adapter's recovery indexes the registered ref, regardless of in-memory lifecycle.
        n.adapter._native_submit_ref_sessions.clear()
        await n.adapter._fail_restart_interrupted_submits()
        assert n.adapter._native_submit_terminals[row['admission_id']]['type'] == 'turn.failed'
    finally:
        api_execution.reset(token)


@pytest.mark.asyncio
async def test_real_native_writer_answers_completion_once(internal_gateway, monkeypatch):
    monkeypatch.setattr('gateway.weave_chat_sync._call',
                        lambda live, items: [{'item_id': item.get('item_id'), 'rev': 1} for item in items])
    monkeypatch.setenv('WEAVE_LIVE_HANDOFF_NOTE', '')
    from agent.relay_runtime import NoopRelayRuntime
    monkeypatch.setattr('agent.relay_runtime.RelayHostRegistry.for_profile',
                        lambda self, profile_key=None, **kw: NoopRelayRuntime(profile_key or 'test', 'isolated test'))
    from tests.waiting import async_wait_until
    g = internal_gateway
    key = await _bind_internal_gateway(g)
    g.adapter._ensure_session_db_async = AsyncMock(return_value=g.db)
    g.adapter._ensure_session_db = lambda: g.db
    await initialize_session_authority(g.runner, profile_id='default', instance_id='wire', db=g.db)
    g.adapter._native_submit_start_timeout_s = 120
    status, receipt = await _submit_internal_gateway(g, 'human', 'human task')
    assert status == 202, receipt
    await async_wait_until(lambda: receipt['native_request_ref'] in g.adapter._native_submit_terminals, timeout=120)
    evt = dict(type='async_delegation', delegation_id='wire-child', session_key=key)
    assert await g.runner._self_post_api_server(g.adapter, 'Completed child result', SESSION_ID, evt)
    await async_wait_until(lambda: any(r['status'] == 'terminal' and r['principal_id'].startswith('automation:') for r in
        list_session_admissions(g.db, session_id=SESSION_ID, pending_only=False)), timeout=120)
    rows = g.db.get_messages(SESSION_ID)
    users = [m for m in rows if m['role'] == 'user']
    assert [m.get('display_kind') for m in users] == [None, 'internal_notification']
    assert len(g.requests) == 2
    assert [m['content'] for m in rows if m['role'] == 'assistant'] == ['reply-1', 'reply-2']
    admitted = next(r for r in list_session_admissions(g.db, session_id=SESSION_ID, pending_only=False)
                    if r['principal_id'].startswith('automation:'))
    assert admitted['admission_id'] in g.adapter._native_submit_terminals
    assert await g.runner._self_post_api_server(g.adapter, 'Completed child result', SESSION_ID, evt)
    assert len(g.requests) == 2
