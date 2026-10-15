import json
from types import SimpleNamespace
from gateway.weave_chat_sync import hub_card
from tools.mcp_tool_handlers import _render_call_tool_result


def test_upstream_dual_emit_deduplication_preserves_answer_card():
    card = {'kind': 'output_blocks', 'blocks': [{'type': 'text', 'text': 'Answer'}]}
    payload = {'card': card}
    result = SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(payload))],
                             structuredContent=payload, isError=False, meta=None)
    raw = _render_call_tool_result(result, 'weave-connectors')
    assert 'structuredContent' not in json.loads(raw)
    assert hub_card('mcp__weave_connectors__show_answer', raw) == card
    assert hub_card('mcp__weave_connectors__provider__show_answer', raw) is None
