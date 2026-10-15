"""Legacy (non-authority) runner event hooks; canonical authority uses observers."""
async def run(runner, event, source, key, generation):
    adapter = runner._adapter_for_source(source)
    started = getattr(adapter, '_on_native_submit_started', None)
    finished = getattr(adapter, '_on_native_submit_finished', None)
    native = bool((getattr(event, 'metadata', None) or {}).get('native_request_ref'))
    try:
        if native and callable(started):
            await started(event, key)
        result = await runner._handle_message_with_agent(event, source, key, generation)
        if native and isinstance(result, dict) and (result.get('failed') or result.get('error') or result.get('completed') is False):
            event.metadata['native_submit_failed'] = True
        return result
    except BaseException:
        if native:
            event.metadata['native_submit_failed'] = True
        raise
    finally:
        if native and callable(finished):
            await finished(event, key)

def record_folded_refs(survivor, incoming):
    meta = survivor.metadata
    refs = [incoming.metadata.get('native_request_ref'), *incoming.metadata.get('merged_native_request_refs', [])]
    own = meta.get('native_request_ref')
    folded = meta.setdefault('merged_native_request_refs', [])
    for ref in refs:
        if isinstance(ref, str) and ref and ref != own and ref not in folded:
            folded.append(ref)


def mark_result(event, result):
    if isinstance(result, dict) and (result.get('failed') or result.get('error') or result.get('completed') is False):
        event.metadata['native_submit_failed'] = True
