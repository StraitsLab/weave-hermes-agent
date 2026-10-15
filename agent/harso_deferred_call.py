"""Exact bare deferred calls go through the same scoped, schema-checked bridge."""
import json


def bridge_bare_deferred_call(agent, call, valid_names):
    if 'tool_call' not in valid_names or call.function.name in valid_names:
        return False
    from agent.tool_executor import _tool_search_scoped_names
    if call.function.name not in _tool_search_scoped_names(agent):
        return False
    try:
        args = call.function.arguments
        args = json.loads(args or '{}') if isinstance(args, str) else args
    except (TypeError, ValueError):
        return False
    if not isinstance(args, dict):
        return False
    call.function.arguments = json.dumps({'name': call.function.name, 'arguments': args})
    call.function.name = 'tool_call'
    return True
