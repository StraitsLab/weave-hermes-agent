"""Native submit admission into the upstream session authority.

Harso's former bot_runtime RPC transport was removed upstream. Keep one FIFO and
one execution owner by using the v0.22 authority, not another runner on the side.
"""
import asyncio
import os
from gateway.config import Platform


def enabled():
    # Cells set gateway.native_bot_sessions in config.yaml (v0.21 read it there); the env switch also counts.
    if os.environ.get("HERMES_NATIVE_BOT_SESSIONS") == "1":  # health: allow HX002 -- Existing Harso installer switch; preserve deployed cell compatibility.
        return True
    from hermes_cli.config import load_config
    return (load_config().get("gateway") or {}).get("native_bot_sessions") is True


def capabilities():
    return {"native_session_inference": enabled(), "session_passive_append_group": True, "hub_revision_submit": True}


async def admit(adapter, profile, session_id, message, ref, external_request_id="", *,
                internal=False, busy_mode="queue", expected_active_ref=None):
    from gateway.session_authorities import active_authority
    authority = active_authority(adapter.gateway_runner)
    if authority is None:
        raise RuntimeError("native session authority unavailable")
    from gateway.session_api_turn import admit_api_turn_async, observe_api_turn, observe_api_controls
    from gateway.session_contract import Principal, SessionRef
    from hermes_state_runtime import list_session_admissions
    target = authority.logical_owner(session_id)
    rows = list_session_admissions(authority.db, session_id=target)
    active = next((row for row in rows if row['status'] == 'started'), None)
    active_ref = active['request_id'] if active else None
    if internal:
        busy_mode = "queue"
    if expected_active_ref is not None and busy_mode != "queue" and active_ref != expected_active_ref:
        return "target_changed"
    actor = Principal('api', authority.profile_id,
        frozenset({'session:submit', 'session:control', 'session:read', 'session:respond', 'session:approve'}),
        'harso-native:' + ref)
    session_ref = SessionRef(authority.profile_id, target)
    if active is not None and busy_mode == 'steer':
        try:
            await authority.steer(actor, session_ref, active['generation'], message)
        except Exception:  # health: allow BLE001,S110 -- HTTP/credential boundary: return typed refusal without exception contents.
            pass  # A closing steer falls back to FIFO, never drops the new input.
        else:
            adapter._native_submit_ref_sessions[ref] = (profile, session_id)
            adapter._record_native_submit_handoff(ref, 'steered')
            adapter._native_submit_event(ref, 'turn.started', steered_into=active_ref)
            task = asyncio.create_task(_observe_steered(adapter, authority, active, ref))
            adapter.__dict__.setdefault('_native_authority_tasks', {})[ref] = task
            return 'steered'
    admitted = await admit_api_turn_async(adapter, session_id=target, user_message=message,
        conversation_history=[], history_from_session=True, request_id=ref,
        relay_metadata={'harso_native_v1': {'internal': bool(internal),
                                         'session_id': session_id, 'run_id': external_request_id}})
    adapter._native_submit_ref_sessions[ref] = (profile, session_id)
    adapter._native_submit_external_ids[ref] = external_request_id
    adapter.__dict__.setdefault('_native_authority_admissions', {})[ref] = admitted
    result = 'queued' if active or any(row['status'] == 'queued' for row in rows) else 'streaming'
    adapter._record_native_submit_handoff(ref, result)
    # observe_api_turn owns registration, scheduling and cleanup. Keep its task alive
    # independently of the HTTP request that got the admission acknowledgement.
    task = asyncio.create_task(_observe(adapter, admitted, ref, external_request_id))
    adapter.__dict__.setdefault('_native_authority_tasks', {})[ref] = task
    if active is not None and busy_mode == 'interrupt':
        await authority.interrupt(actor, session_ref, active['generation'])
    return result


async def _observe_steered(adapter, authority, active, ref):
    from hermes_state_runtime import get_session_admission
    try:
        while True:
            row = get_session_admission(authority.db, admission_id=active['admission_id'])
            if row is None or row['status'] in ('terminal', 'unknown'):
                break
            await asyncio.sleep(.05)
        adapter._native_submit_close(ref, 'turn.completed' if row and row['status'] == 'terminal' else 'turn.failed')
    except BaseException:
        adapter._native_submit_close(ref, 'turn.failed')
        raise


async def _observe(adapter, admitted, ref, external_request_id):
    from gateway.session_api_turn import observe_api_turn, observe_api_controls
    def emit(kind, **fields):
        adapter._native_submit_event(ref, kind, **fields)
    def control(kind, prompt):
        if kind == 'approval.request':
            emit('approval.request', **adapter._native_approval_projection({**prompt, 'request_id': prompt['prompt_id']}))
        elif kind == 'clarify.settled':
            adapter._native_submit_clarifies[(ref, prompt['prompt_id'])] = 'terminal'
            adapter._native_submit_events.pop(ref, None)
        elif kind == 'clarify.request':
            cid = prompt['prompt_id']
            adapter._native_submit_clarifies[(ref, cid)] = 'pending'
            data = {'clarify_id': cid, 'question': prompt.get('question', ''),
                    'choices': prompt.get('choices') or [], 'multi_select': prompt.get('multi_select', False)}
            adapter._native_submit_events[ref] = [dict(type='clarify.request', native_request_ref=ref, **data)]
            emit('clarify.request', **data)
    try:
        emit('turn.started')
        def tool_complete(cid, name, args, result):
            from gateway.native_session_cards import hub_card
            from agent.display import _detect_tool_failure
            failed, _ = _detect_tool_failure(str(name or 'tool'), result)
            card = None if failed else hub_card(name, result)
            emit('tool.failed' if failed else 'tool.completed', tool_call_id=cid, tool_name=name,
                 **({'card': card} if card else {}))
        with observe_api_controls(admitted, control):
            result, _usage = await observe_api_turn(admitted,
                stream_delta_callback=lambda text: emit('assistant.delta', delta=text[:4096]) if text else None,
                tool_start_callback=lambda cid, name, args: emit('tool.started', tool_call_id=cid, tool_name=name),
                tool_complete_callback=tool_complete)
        content = result.get('final_response')
        if content:
            emit('assistant.final', content=content[:16384], **({'external_request_id': external_request_id} if external_request_id else {}))
        failed = bool(result.get('failed') or result.get('error') or result.get('interrupted') or result.get('completed') is False)
        adapter._native_submit_close(ref, 'turn.failed' if failed else 'turn.completed')
    except BaseException:
        adapter._native_submit_close(ref, 'turn.failed')
        raise
    finally:
        adapter.__dict__.get('_native_authority_tasks', {}).pop(ref, None)


def pending(adapter, ref, kind):
    admitted = adapter.__dict__.get('_native_authority_admissions', {}).get(ref)
    if admitted is None:
        return None
    from hermes_state_runtime import get_session_admission
    authority, session_ref, row = admitted
    current = get_session_admission(authority.db, admission_id=row['admission_id'])
    live = authority.sessions.get(session_ref.session_id)
    if live is None or not current or current['status'] != 'started':
        return []
    return [prompt for prompt in live.controls.snapshot(session_ref.session_id, current['generation'])
            if prompt['kind'] == kind]


async def respond(adapter, ref, prompt_id, value, kind):
    admitted = adapter.__dict__.get('_native_authority_admissions', {}).get(ref)
    if admitted is None:
        return None
    from hermes_state_runtime import get_session_admission
    from gateway.session_contract import Principal
    authority, session_ref, row = admitted
    current = get_session_admission(authority.db, admission_id=row['admission_id'])
    if current is None or current['status'] != 'started':
        return False
    actor = Principal('api', authority.profile_id, frozenset({'session:read', 'session:approve', 'session:respond'}), 'harso-native:' + ref)
    snapshot = await authority.attach(actor, session_ref)
    try:
        if not any(p['prompt_id'] == prompt_id and p['kind'] == kind for p in snapshot.prompts):
            return False
        result = await authority.respond(actor, session_ref, current['generation'], prompt_id,
            {'choice' if kind == 'approval' else 'answer': value}, kind=kind)
    except Exception:  # health: allow BLE001,S110 -- HTTP/credential boundary: return typed refusal without exception contents.
        return False
    finally:
        await authority.detach(actor, snapshot.subscription_id)
    return result.get('status') == 'resolved'


def internal_presentation(event):
    """Only this admitted turn's persisted provenance types a Harso notification.

    Upstream API admissions set internal=True for trusted ingress even for human
    messages; that transport trust bit must not make every Harso message hidden.
    """
    from gateway.session_api_turn import api_execution
    current = api_execution.get()
    marker = ((current or {}).get('relay_metadata') or {}).get('harso_native_v1')
    if not isinstance(marker, dict) or type(marker.get('internal')) is not bool:
        return None
    return 'internal_notification' if marker['internal'] else ''
