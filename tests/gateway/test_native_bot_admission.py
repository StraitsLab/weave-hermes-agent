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
