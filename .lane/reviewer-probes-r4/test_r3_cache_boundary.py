"""Probe first composition of newly fetched memory after a live OFF edit."""
import json
import threading
import pytest
from test_harso_copilot import _switch, _provider, _manager, _capture, _DELIVERY
from test_turn_context import _FakeAgent, _build


@pytest.mark.parametrize('start_on,end_on', [(True, False), (True, True), (False, False), (False, True)])
@pytest.mark.parametrize('representation', ['delivery', 'items'])
def test_new_prefetch_cache_not_first_composed_after_off(monkeypatch, start_on, end_on, representation):
    _switch(start_on)
    p = _provider(monkeypatch)
    m = _manager(p)
    response = {'recall_status': 'ok', 'items': [{'citation': '[harso: e1]', 'text': 'CACHE_SENTINEL'}]}
    if representation == 'delivery':
        response['delivery'] = _DELIVERY
    seen = _capture(monkeypatch, {'/context': response})
    agent = _FakeAgent()
    agent._memory_manager = m
    monkeypatch.setattr('agent.auxiliary_client.set_runtime_main', lambda *a, **k: None)
    # Recall indicator is between prefetch and first api_content composition in the real prologue.
    # Pause here to deterministically schedule the same external config edit as the in-flight probes.
    reached, resume = threading.Event(), threading.Event()
    original_describe = m.describe_recall
    def paused_describe():
        reached.set()
        assert resume.wait(3)
        return original_describe()
    monkeypatch.setattr(m, 'describe_recall', paused_describe)
    def toggle():
        assert reached.wait(3)
        _switch(end_on)
        resume.set()
    controller = threading.Thread(target=toggle)
    controller.start()
    ctx = _build(agent, user_message='What do you remember about my dentist appointment?')
    controller.join(3)
    row = ctx.messages[ctx.current_turn_user_idx]
    rendered = any(marker in row.get('api_content', '') for marker in ['Harso memory delivery', 'CACHE_SENTINEL'])
    print(f'start_on={start_on} end_on={end_on} representation={representation} requests={len(seen)} first_sidecar_contains_memory={rendered} plain_text_kept={row["content"] == ctx.user_message}')
    assert len(seen) == 1 and p.copilot_active() == end_on
    assert row['content'] == ctx.user_message
    if start_on and not end_on:
        assert not rendered, 'newly fetched ON cache first composed after OFF must not deliver copilot content'
    else:
        assert rendered, 'ON and legacy-OFF controls must remain usable'
