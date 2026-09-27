import pytest
import agent.conversation_loop as loop
from tests.agent.test_api_content_sidecar import wire_env, _chat_requests, _user_messages, _tc_resp, _text_resp
from tests.plugins.memory.test_harso_copilot import _switch, _provider, _manager, _capture
from tests.plugins.memory.test_harso_copilot import _DELIVERY
_QUESTION = 'What do you remember about my dentist appointment?'
def _response(representation):
    response = {'recall_status': 'ok', 'items': [{'citation': '[harso: e1]', 'text': 'CACHE_SENTINEL'}]}
    if representation == 'delivery':
        response['delivery'] = _DELIVERY
    return response

def _has_memory(text):
    return any(marker in text for marker in ('Harso memory delivery', 'CACHE_SENTINEL'))

@pytest.mark.parametrize('unstamped', [False, True])
@pytest.mark.parametrize('representation', ['delivery', 'items'])
@pytest.mark.parametrize('first_on', [False, True])
def test_repeated_passes_switch_flip(monkeypatch, wire_env, unstamped, representation, first_on):
    make_agent, handler, db, sid = wire_env
    _switch(True)
    provider = _provider(monkeypatch)
    manager = _manager(provider)
    _capture(monkeypatch, {'/context': _response(representation)})
    real_describe = manager.describe_recall
    def describe():
        _switch(first_on)
        return real_describe()
    monkeypatch.setattr(manager, 'describe_recall', describe)
    aggregate_inputs = []
    if unstamped:
        import copy
        def aggregate(**kw):
            aggregate_inputs.append(copy.deepcopy(kw['api_messages']))
            return ''  # No external MoA inference; capture its real input boundary.
        monkeypatch.setattr('agent.moa_loop.aggregate_moa_context', aggregate)
    agent = make_agent()
    agent._memory_manager = manager
    real_execute = agent._execute_tool_calls
    def execute(*args, **kwargs):
        _switch(not first_on)
        return real_execute(*args, **kwargs)
    monkeypatch.setattr(agent, '_execute_tool_calls', execute)
    handler.response_queue.extend([_tc_resp('read_file', '{"file_path":"/nonexistent-path"}'), _text_resp('done')])
    loop.run_conversation(agent, _QUESTION, conversation_history=[], task_id='r4-hunt', moa_config={'reference_models': []} if unstamped else None)
    reqs = _chat_requests(handler)
    if unstamped:
        assert len(aggregate_inputs) == 2
        assert all(_has_memory(next(m['content'] for m in a if m['role'] == 'user')) == first_on for a in aggregate_inputs)
    assert len(reqs) == 2
    texts = [_user_messages(r)[0]['content'] for r in reqs]
    assert all(t.startswith(_QUESTION) and 'PLUGIN-CTX' in t for t in texts)
    assert all(_has_memory(t) == first_on for t in texts)
    print(f'path={"unstamped" if unstamped else "stamped"} repr={representation} first_on={first_on} memory_kept={first_on} bytes_equal={texts[0] == texts[1]}')
    if texts[0] != texts[1]:
        import difflib, hashlib, json
        from pathlib import Path
        print('FIRST_SHA256=' + hashlib.sha256(texts[0].encode()).hexdigest())
        print('SECOND_SHA256=' + hashlib.sha256(texts[1].encode()).hexdigest())
        print(''.join(difflib.unified_diff(texts[0].splitlines(True), texts[1].splitlines(True), fromfile='first_request', tofile='second_request')))
        Path('/tmp/rv-k-fork-client-r4/requests-' + representation + '.json').write_text(json.dumps(texts, indent=2))
    assert texts[0] == texts[1]
