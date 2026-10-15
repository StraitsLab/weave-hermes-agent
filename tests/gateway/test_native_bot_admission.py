"""Native cells use gateway configuration and classify unsuccessful results."""
from gateway.native_session_lifecycle import failed_result
from gateway.native_session_cards import hub_card
import pytest


@pytest.mark.parametrize('result', [{'failed': True}, {'error': 'no auth'}, {'completed': False}, {'interrupted': True}])
def test_unsuccessful_results_never_complete(result):
    assert failed_result(result)


def test_card_projection_is_allowlisted():
    card = {'kind': 'output_blocks', 'major': 1}
    assert hub_card('mcp__weave_connectors__show_flight_status', {'structuredContent': {'card': card}}) == card
    assert hub_card('mcp__weave_connectors__acme__lookup', {'structuredContent': {'card': card}}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('authenticated', [False, True])
@pytest.mark.parametrize('fails', [False, True])
async def test_vault_identity_follows_authenticated_native_turn(authenticated, fails, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from agent.vault_backends.weave import _run_context
    from agent.vault_backends.base import VaultUseRefused
    from gateway.native_bot_admission import native_turn_context
    from gateway.native_session_lifecycle import run

    run_id = '0199aaaa-0000-7000-8000-000000000001'
    event = SimpleNamespace(metadata={
        'native_request_ref': 'ref', 'native_submit_authenticated': authenticated,
        'gateway_session_id': 'native-session', 'external_request_id': run_id,
    })
    monkeypatch.setenv('WEAVE_HERMES_SESSION_ID', 'stale-session')

    async def handle(*args):
        if authenticated:
            assert await asyncio.to_thread(_run_context) == {'session_id': 'native-session', 'run_id': run_id}
        else:
            with pytest.raises(VaultUseRefused):
                await asyncio.to_thread(_run_context)
        if fails:
            raise RuntimeError('turn failed')
        return {'completed': True}

    runner = SimpleNamespace(_delivery_adapter_for=lambda _: None, _handle_message_with_agent=handle)
    token = native_turn_context.set({'session_id': 'outer', 'run_id': run_id})
    try:
        if fails:
            with pytest.raises(RuntimeError, match='turn failed'):
                await run(runner, event, None, 'key', 1)
        else:
            await run(runner, event, None, 'key', 1)
        assert native_turn_context.get() == {'session_id': 'outer', 'run_id': run_id}
    finally:
        native_turn_context.reset(token)
