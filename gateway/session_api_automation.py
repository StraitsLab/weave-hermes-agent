"""Native API consumers on the existing automation FIFO and native chat writer."""
import json

from hermes_state_runtime import RuntimeStoreError


def native_binding(authority, session_id):
    from gateway.session_api import _BINDING_PREFIX
    owner = authority.logical_owner(session_id)
    with authority.db._read_ctx() as conn:
        row = conn.execute('SELECT value FROM state_meta WHERE key=?', (_BINDING_PREFIX + owner,)).fetchone()
    return json.loads(row[0]) if row else {}


def check_native_consumer(authority, session_id):
    from gateway.session_api import restore_api_session
    binding = native_binding(authority, session_id)
    if binding.get('native_history_delivery') is not True:
        raise RuntimeStoreError('permission_denied')
    ref = restore_api_session(authority, binding['session_id'])
    return ref, authority.sessions[ref.session_id]


def snapshot(authority, adapter, event, identity, entry, notification):
    ref, live = check_native_consumer(authority, entry.session_id)
    runner = authority.runner
    if (runner._session_key_for_source(event.source) != live.route
            or runner._adapter_for_source(live.source) is not adapter):
        raise RuntimeStoreError('admission_conflict')
    source = live.source.to_dict()
    source.pop('message_id', None)
    source['is_bot'] = False
    if event.metadata.get('automation_identities'):
        notification['identities'] = sorted(set(event.metadata['automation_identities']))
    return {'text': event.text, 'native_text_v1': {
        'source': source, 'route': live.route, 'timestamp': '1970-01-01T00:00:00+00:00',
        'event': {'message_id': identity},
        'automation': {'identity': identity, 'owner': ref.session_id, **notification}}}, entry


async def deliver(runner, adapter, text, session_id, evt):
    """False only for non-native API consumers; native refusals propagate for producer retry."""
    from gateway.session_authorities import active_authority
    from gateway.platforms.event import MessageEvent
    from gateway.session_automation import admit_process_notice
    authority = active_authority(runner)
    if authority is None or not native_binding(authority, session_id).get('native_history_delivery'):
        return False
    ref, live = check_native_consumer(authority, session_id)
    event = MessageEvent(text=text, source=live.source, internal=True, metadata={
        'gateway_session_key': live.route, 'gateway_session_id': authority.physical_target(ref)})
    if evt.get('_automation_identities'):
        event.metadata['automation_identities'] = evt['_automation_identities']
    await admit_process_notice(runner, authority, adapter, event, {**evt, 'session_key': live.route})
    return True


async def bind_native(runner, session_id):
    from gateway.session_authorities import active_authority
    from gateway.session_api import bind_api_steps, run_steps_off_loop
    authority = active_authority(runner)
    if authority is not None:
        await run_steps_off_loop(authority, bind_api_steps(authority, session_id, native_history_delivery=True))
    return authority


async def prepare_event(authority, ref, row, event):
    """Prepare trusted automation through the native lifecycle and runtime lock."""
    import asyncio
    import hashlib
    from gateway.session_api_turn import api_execution
    _ref, live = check_native_consumer(authority, ref.session_id)
    adapter = authority.runner._adapter_for_source(live.source)
    sid = authority.physical_target(ref)
    from gateway.platforms.api_server_native_model_lock import apply_native_model_lock
    await apply_native_model_lock(adapter, sid, event.source, live.route)
    native_ref = row['admission_id']
    await asyncio.to_thread(authority.db.register_native_session_submit, ref.session_id,
        external_request_id='automation:' + native_ref,
        message_sha256=hashlib.sha256(event.text.encode()).hexdigest(), native_request_ref=native_ref)
    config = await asyncio.to_thread(authority.db.get_session_model_config, sid)
    request = adapter._runtime_request_from_persisted_session_lock({'model_config': config}, {})
    settings = {'session_history_delivery': '1', 'async_delivery': True}
    if request:
        settings.update(confirmed_runtime_lock=True, requested_model=request['requested']['model'],
            requested_provider=request['requested']['provider'], requested_runtime=request['requested'],
            route=request['route'], route_source=request['route_source'], model_options=request['model_options'])
    api_execution.set({'adapter': adapter, 'settings': settings, 'history': None, 'content': event.text,
                       'native_history_delivery': True,
                       'authority': authority, 'ref': ref, 'row': row,
                       'turn_author': {'id': live.source.user_id or None, 'name': live.source.user_name or None, 'is_bot': False}})
    event.metadata.update(native_request_ref=native_ref, native_submit_authenticated=True, native_automation=True)
    event.allow_gateway_control = False


def recover(adapter, authority):
    from hermes_state_runtime import list_session_admissions
    with authority.db._read_ctx() as conn:
        targets = [r[0] for r in conn.execute(
            "SELECT DISTINCT target_session_id FROM session_admissions WHERE principal_id LIKE 'automation:%' AND status='queued'")]
    for sid in targets:
        if not native_binding(authority, sid).get('native_history_delivery'):
            continue
        try:
            ref, _live = check_native_consumer(authority, sid)
            if not any(r['status'] == 'unknown' for r in list_session_admissions(authority.db, session_id=sid)):
                authority._schedule(ref)
        except RuntimeStoreError:
            # As for API input, a refused route remains queued, never aborts adapter startup.
            import logging
            logging.getLogger(__name__).warning('Native API automation session %s paused', sid)
