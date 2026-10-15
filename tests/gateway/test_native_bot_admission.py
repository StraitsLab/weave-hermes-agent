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
