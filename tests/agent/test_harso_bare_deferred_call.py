"""Harso bare-call compatibility without fuzzy repair or toolset escalation."""
import json

from agent.harso_deferred_call import bridge_bare_deferred_call


def test_bare_call_preserves_exact_identity_and_arguments(monkeypatch):
    monkeypatch.setattr('agent.tool_executor._tool_search_scoped_names', lambda _: frozenset({'mcp__hub__send'}))
    class Function:
        name = 'mcp__hub__send'
        arguments = '{"target":"x"}'
    call = type('Call', (), {'function': Function()})()
    assert bridge_bare_deferred_call(object(), call, {'tool_call'})
    assert call.function.name == 'tool_call'
    assert json.loads(call.function.arguments) == {'name': 'mcp__hub__send', 'arguments': {'target': 'x'}}


def test_out_of_scope_and_malformed_calls_are_not_repaired(monkeypatch):
    monkeypatch.setattr('agent.tool_executor._tool_search_scoped_names', lambda _: frozenset({'allowed'}))
    for name, args in [('other', '{}'), ('allowed', 'bad'), ('allowed', '[]')]:
        call = type('Call', (), {'function': type('Function', (), {'name': name, 'arguments': args})()})()
        assert not bridge_bare_deferred_call(object(), call, {'tool_call'})
        assert call.function.name == name
