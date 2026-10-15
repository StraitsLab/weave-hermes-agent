"""Harso compatibility projections over upstream authority observers."""
from contextlib import contextmanager
from types import SimpleNamespace
import pytest


@pytest.mark.asyncio
async def test_authority_tool_complete_only_projects_allowlisted_cards(monkeypatch):
    from gateway import native_bot_admission as native
    from gateway import session_api_turn
    seen = []
    adapter = SimpleNamespace(_native_submit_event=lambda ref, kind, **fields: seen.append((kind, fields)),
                              _native_submit_close=lambda *a: None)
    card = {'kind': 'output_blocks', 'major': 1}
    @contextmanager
    def controls(*args):
        yield
    async def observe(admitted, **callbacks):
        for name in ['mcp__weave_connectors__show_flight_status', 'mcp__weave_connectors__acme__lookup']:
            callbacks['tool_complete_callback']('c1', name, {}, {'structuredContent': {'card': card}})
        return {'final_response': 'ok'}, {}
    monkeypatch.setattr(session_api_turn, 'observe_api_controls', controls)
    monkeypatch.setattr(session_api_turn, 'observe_api_turn', observe)
    await native._observe(adapter, (None, None, None), 'r1', 'external')
    tools = [fields for kind, fields in seen if kind == 'tool.completed']
    assert tools[0]['card'] == card
    assert 'card' not in tools[1]
    from gateway.native_session_cards import hub_card
    import json
    assert hub_card('mcp__weave_connectors__show_flight_status', {'structuredContent': json.dumps({'card': card})}) == card


def test_internal_display_uses_admitted_provenance_not_transport_trust():
    from gateway.native_bot_admission import internal_presentation
    from gateway.session_api_turn import api_execution
    token = api_execution.set({'relay_metadata': {'harso_native_v1': {'internal': False}}})
    try:
        assert internal_presentation(SimpleNamespace(internal=True)) == ''
        api_execution.set({'relay_metadata': {'harso_native_v1': {'internal': True}}})
        assert internal_presentation(None) == 'internal_notification'
    finally:
        api_execution.reset(token)


@pytest.mark.asyncio
async def test_native_admission_carries_vault_identity_not_model_arguments(monkeypatch):
    from unittest.mock import AsyncMock, Mock
    from gateway import native_bot_admission as native, session_api_turn
    from gateway import session_authorities
    admitted = (None, None, None)
    admit = AsyncMock(return_value=admitted)
    authority = Mock()
    authority.logical_owner.return_value = "session"
    monkeypatch.setattr("hermes_state_runtime.list_session_admissions", lambda *a, **kw: [])
    monkeypatch.setattr(session_authorities, "active_authority", lambda _: authority)
    monkeypatch.setattr(session_api_turn, "admit_api_turn_async", admit)
    async def observe(*args):
        return None
    monkeypatch.setattr(native, "_observe", observe)
    adapter = SimpleNamespace(gateway_runner=object(), _native_submit_ref_sessions={},
                              _native_submit_external_ids={}, _record_native_submit_handoff=Mock(),
                              _native_submit_event=Mock())
    run = "0199aaaa-0000-7000-8000-000000000001"
    assert await native.admit(adapter, "default", "session", "sign in", "native-ref", run) == "streaming"
    assert admit.call_args.kwargs["relay_metadata"]["harso_native_v1"] == dict(
        internal=False, session_id="session", run_id=run)
