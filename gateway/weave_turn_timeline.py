"""Partition existing sanitized step records by their next visible assistant message."""


def chronological_steps(messages, projections):
    from gateway.weave_chat_sync import _is_browser_card, _steps_turn_boundary, row_key

    details = {}
    turn = []
    for message in [*messages, {'role': 'user', 'content': ''}]:
        if _steps_turn_boundary(message):
            local = next((projections[row_key(m)].get('steps', []) for m in reversed(turn)
                          if row_key(m) in projections), [])
            step_map = {step['id']: step for step in local}
            details.update({id(m): step_map for m in turn})
            turn = []
        turn.append(message)
    pending = []
    for message in messages:
        local = details.get(id(message), {})
        if _steps_turn_boundary(message):
            pending = []
        if message.get('role') != 'assistant' or message.get('display_kind') in ('hidden', 'internal_notification'):
            continue
        content = message.get('content')
        if isinstance(content, str) and content.strip() and not _is_browser_card(message):
            body = projections.setdefault(row_key(message), {})
            body['steps'] = [local[call] for call in dict.fromkeys(pending) if call in local]
            if body['steps']:
                body['steps_before_text'] = True
            pending = []
        reasoning = None if content else message.get('reasoning') or message.get('reasoning_content')
        for call in message.get('tool_calls') or ():
            if isinstance(call, dict) and call.get('id') in local:
                if isinstance(reasoning, str) and reasoning.strip():
                    local[call['id']]['thinking_key'] = row_key(message)
                    reasoning = None
                pending.append(call['id'])
    return projections
