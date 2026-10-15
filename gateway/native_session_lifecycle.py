"""Native request lifecycle on the upstream gateway writer."""
async def run(runner, event, source, key, generation):
    adapter = runner._delivery_adapter_for(source)
    started = getattr(adapter, '_on_native_submit_started', None)
    finished = getattr(adapter, '_on_native_submit_finished', None)
    metadata = getattr(event, 'metadata', None) or {}
    native = bool(metadata.get('native_request_ref'))
    from gateway.native_bot_admission import native_turn_context
    token = native_turn_context.set(
        {'session_id': metadata.get('gateway_session_id'), 'run_id': metadata.get('external_request_id')}
        if native and metadata.get('native_submit_authenticated') is True else None
    )
    try:
        if native and callable(started):
            await started(event, key)
        result = await runner._handle_message_with_agent(event, source, key, generation)
        if native and failed_result(result):
            event.metadata['native_submit_failed'] = True
        return result
    except BaseException:
        if native:
            event.metadata['native_submit_failed'] = True
        raise
    finally:
        native_turn_context.reset(token)
        if native and callable(finished):
            await finished(event, key)

def record_folded_refs(survivor, incoming):
    meta = survivor.metadata
    refs = [incoming.metadata.get('native_request_ref'), *incoming.metadata.get('merged_native_request_refs', [])]
    refs = [ref for ref in refs if isinstance(ref, str) and ref]
    if not refs:
        return
    own = meta.get('native_request_ref')
    folded = meta.setdefault('merged_native_request_refs', [])
    for ref in refs:
        if isinstance(ref, str) and ref and ref != own and ref not in folded:
            folded.append(ref)


def mark_result(event, result):
    if failed_result(result):
        event.metadata['native_submit_failed'] = True


def wire_tools(agent, adapter, session_key):
    native_start = getattr(adapter, '_native_submit_tool_started', None)
    native_complete = getattr(adapter, '_native_submit_tool_completed', None)
    previous_start, previous_complete = agent.tool_start_callback, agent.tool_complete_callback
    if callable(native_start):
        def start(call_id, name, args):
            if previous_start:
                previous_start(call_id, name, args)
            native_start(session_key, call_id, name)
        agent.tool_start_callback = start
    if callable(native_complete):
        def complete(call_id, name, args, result):
            if previous_complete:
                previous_complete(call_id, name, args, result)
            native_complete(session_key, call_id, name, result)
        agent.tool_complete_callback = complete


def wire_delta(adapter, session_key, previous):
    native = getattr(adapter, '_native_submit_delta', None)
    if not callable(native):
        return previous
    def delta(text):
        if previous is not None:
            previous(text)
        native(session_key, text)
    return delta


def failed_result(result):
    return isinstance(result, dict) and bool(
        result.get('failed') or result.get('error') or result.get('interrupted')
        or result.get('completed') is False
    )
