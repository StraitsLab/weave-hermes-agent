"""Executor timing survives persistence without becoming model context."""

import json

import pytest

from agent.tool_executor import _ToolCallRef, _commit_tool_result
from agent.turn_context import build_api_messages
from tests.agent.test_tool_call_incremental_persistence import _attach_real_session_db, _make_agent
from tools.budget_config import DEFAULT_BUDGET


@pytest.mark.parametrize('callback_metadata', [None, {'kind': 'x'}])
def test_committed_duration_is_durable_and_display_only(tmp_path, monkeypatch, callback_metadata):
    monkeypatch.setattr('agent.title_generator.maybe_auto_title', lambda *a, **kw: None)
    agent = _make_agent()
    agent._current_turn_timestamp = 1.0
    agent.tool_result_metadata_callback = lambda *a: callback_metadata
    db = _attach_real_session_db(agent, tmp_path / 'duration.db', 'timed-tools')
    messages = [{'role': 'user', 'content': 'Check'}]
    try:
        for call_id, duration in [('a', 0.05), ('b', 0.4)]:
            messages.append({'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': call_id, 'type': 'function', 'function': {
                    'name': 'web_search', 'arguments': json.dumps({'query': 'example'})}}]})
            result = _commit_tool_result(
                agent, messages, _ToolCallRef('web_search', {'query': 'example'}, 'timed-tools', call_id, []),
                'ok', budget=DEFAULT_BUDGET, tool_duration=duration,
                is_error=False, blocked=False, effect_disposition=None,
            )
            assert result is not None
        expected = [{**(callback_metadata or {}), 'duration_ms': ms} for ms in (50, 400)]
        assert [m.get('display_metadata') for m in messages if m['role'] == 'tool'] == expected
        durable = db.get_messages_as_conversation('timed-tools')
        assert [m.get('display_metadata') for m in durable if m['role'] == 'tool'] == expected
        api_messages, _ = build_api_messages(
            agent, messages, current_turn_user_idx=0, ext_prefetch_cache='',
            plugin_user_context='', moa_config=None, active_system_prompt='fixed prefix',
        )
        assert len([m for m in api_messages if m['role'] == 'tool']) == 2
        assert all('display_metadata' not in m for m in api_messages)
        assert [m.get('display_metadata') for m in messages if m['role'] == 'tool'] == expected
    finally:
        db.close()


@pytest.mark.parametrize('existing_metadata', [None, {'kind': 'x'}])
def test_blocked_commit_has_no_duration_and_preserves_existing_metadata(monkeypatch, existing_metadata):
    import agent.tool_executor as executor

    agent = _make_agent()
    agent._current_turn_timestamp = 1.0
    callback_calls = []
    agent.tool_result_metadata_callback = lambda *a: callback_calls.append(a)
    original = executor.make_tool_result_message

    def make_message(*args, **kwargs):
        message = original(*args, **kwargs)
        if existing_metadata is not None:
            message['display_metadata'] = dict(existing_metadata)
        return message

    monkeypatch.setattr(executor, 'make_tool_result_message', make_message)
    messages = []
    result = _commit_tool_result(
        agent, messages, _ToolCallRef('terminal', {'command': 'git status'}, 'blocked', 'a', []),
        'blocked', budget=DEFAULT_BUDGET, tool_duration=0.4,
        is_error=True, blocked=True, effect_disposition=None,
    )
    assert result is not None
    assert messages[-1].get('display_metadata') == existing_metadata
    assert 'duration_ms' not in (messages[-1].get('display_metadata') or {})
    assert callback_calls == []  # blocked calls do not emit completion metadata callbacks
