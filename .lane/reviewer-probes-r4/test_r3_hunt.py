"""Independent bounded F4 sibling hunt; fake service, real manager/config/provider."""
import importlib.util
import json
import subprocess
import threading
from pathlib import Path
import pytest
from test_harso_copilot import _switch, _provider, _manager, _capture, _DELIVERY

RESPONSE = {'recall_status': 'ok', 'delivery': _DELIVERY,
            'items': [{'text': 'R3_SENTINEL', 'citation': '[harso: e1]'}],
            'routing_hint': 'Routing hint: inline (0.91)'}

@pytest.mark.parametrize('timeout', [False, True])
def test_real_thread_prefetch_off_completion_never_cached(monkeypatch, timeout):
    _switch(True)
    p = _provider(monkeypatch)
    m = _manager(p)
    m.set_external_prefetch_timeout(0.01 if timeout else 2)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def post(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return RESPONSE
    monkeypatch.setattr(p, '_post', post)
    if timeout:
        assert m.prefetch_all('What do you remember about tea?') == ''
        assert entered.is_set()
        _switch(False)
        release.set()
        worker = m._external_prefetch_threads[p.name]
        worker.join(3)
        assert not worker.is_alive()
    else:
        def toggle():
            assert entered.wait(3)
            _switch(False)
            release.set()
        controller = threading.Thread(target=toggle)
        controller.start()
        result = m.prefetch_all('What do you remember about tea?')
        controller.join(3)
        assert result == RESPONSE['routing_hint']
    assert calls == [1]
    # Fresh OFF turn makes only the unchanged legacy request; the discarded ON response is not replayed.
    monkeypatch.setattr(p, '_post', type(p)._post.__get__(p))
    seen = _capture(monkeypatch, {'/context': {'items': []}})
    assert m.prefetch_all('What do you remember about tea?') == ''
    assert len(seen) == 1 and 'visible_seqs' not in seen[0]['body']
    print(f'timeout={timeout} late_completion_reused=False next_off_request_legacy=True')

@pytest.mark.parametrize('enabled', [False, True])
def test_background_prefetch_is_inherited_noop(monkeypatch, enabled):
    from agent.memory_provider import MemoryProvider
    _switch(enabled)
    p = _provider(monkeypatch)
    m = _manager(p)
    seen = _capture(monkeypatch, {})
    assert p.queue_prefetch.__func__ is MemoryProvider.queue_prefetch
    m.queue_prefetch_all('What tea do I like?')
    m.flush_pending(3)
    assert seen == []
    assert p.fetch_mid_turn_delivery() == '' if not enabled else True
    assert seen == []
    print(f'background_enabled={enabled} requests=0')

@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('representation', ['delivery', 'items', 'denied'])
def test_provider_bytes_equal_r2_when_switch_stable(monkeypatch, enabled, representation):
    import plugins.memory.harso as current
    _switch(enabled)
    p = _provider(monkeypatch)
    source = subprocess.check_output(['git', 'show', 'ffb4e332300d1d2622373d7af9e6f48059a9430e:plugins/memory/harso/__init__.py'])
    # Relative imports inside renderer helpers require the package name.
    scope = {'__name__': 'plugins.memory.harso.r2_comparison', '__package__': 'plugins.memory.harso'}
    exec(compile(source, '<r2-provider>', 'exec'), scope)
    old = scope['HarsoMemoryProvider']()
    old.initialize(p._session_id)
    response = dict(RESPONSE)
    if representation == 'items':
        response.pop('delivery')
    if representation == 'denied':
        response['recall_status'] = 'unavailable'
    seen = _capture(monkeypatch, {'/context': response, '/late-deliveries': response, '/memory-tool': response})
    outputs = []
    for provider in [old, p]:
        start = len(seen)
        provider.note_visible_seqs([4, 2])
        value = [provider.prefetch('What tea do I like?'), provider.fetch_mid_turn_delivery(visible_seqs=[2])]
        value += [provider.handle_tool_call('harso_memory', {'action':action, **args}) for action,args in [('brief',{}),('search',{'query':'tea'}),('open',{'ref':'m7'})]]
        outputs.append((value, seen[start:]))
    assert outputs[0] == outputs[1]
    print(f'stable_on={enabled} representation={representation} r2_head_output_and_requests_equal=True')
