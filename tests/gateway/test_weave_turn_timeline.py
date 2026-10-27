"""Turn chronology uses native message order, not completion wall clocks."""
from gateway.weave_chat_sync import visible_rows


def test_steps_attach_to_next_spoken_segment_in_native_order():
    messages = [
        {"id": 1, "role": "user", "content": "Check"},
        {"id": 2, "role": "assistant", "content": "First", "tool_calls": [
            {"id": "read", "function": {"name": "read_file", "arguments": '{"path":"/private/a.txt"}'}}]},
        {"id": 3, "role": "tool", "tool_call_id": "read", "content": "secret output"},
        {"id": 4, "role": "assistant", "content": "Next", "tool_calls": [
            {"id": "run", "function": {"name": "terminal", "arguments": '{"command":"printf secret"}'}}]},
        {"id": 5, "role": "tool", "tool_call_id": "run", "content": "output"},
        {"id": 6, "role": "assistant", "content": "Done"},
    ]
    for message in messages:
        message['timestamp'] = message['id']
    rows = visible_rows(messages)
    by_text = {row['body']['text']: row['body'] for row in rows}
    assert [s['id'] for s in by_text['Next']['steps']] == ['read']
    assert [s['id'] for s in by_text['Done']['steps']] == ['run']
    assert by_text['Next']['steps_before_text'] is True
    assert by_text['Done']['steps_before_text'] is True
    assert 'secret' not in str(rows)


def test_thinking_is_a_reference_not_text_in_supabase():
    rows = visible_rows([
        {'role': 'user', 'content': 'check', 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'timestamp': 2, 'reasoning': 'private thinking',
         'tool_calls': [{'id': 'read', 'function': {'name': 'read_file', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'read', 'content': 'done', 'timestamp': 3},
        {'role': 'assistant', 'content': 'Done', 'timestamp': 4},
    ])
    assert rows[-1]['body']['steps'][0]['thinking_key'] == 'assistant:2'
    assert 'private thinking' not in str(rows)
