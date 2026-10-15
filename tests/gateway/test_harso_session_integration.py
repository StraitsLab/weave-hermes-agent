"""Native API feature detection and optional chat writer delivery seams."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from gateway.platforms import api_server_harso_sessions as native
from tests.gateway.test_api_server import _make_adapter, _create_app


@pytest.mark.asyncio
async def test_capabilities_advertise_native_consumer_contract(monkeypatch):
    monkeypatch.setenv('HERMES_NATIVE_BOT_SESSIONS', '1')
    adapter = _make_adapter()
    async with TestClient(TestServer(_create_app(adapter))) as client:
        response = await client.get('/v1/capabilities')
        data = await response.json()
    assert data['features']['native_session_inference'] is True
    assert data['features']['session_passive_append_group'] is True
    assert data['features']['hub_revision_submit'] is True


def test_cron_routes_use_retained_handlers():
    from functools import partial
    from gateway.platforms import api_server_harso_cron as cron
    routes = _make_adapter()._http_route_table()
    for method, path, expected in cron._http_routes(_make_adapter()):
        matches = [handler for m, p, handler in routes if (m, p) == (method, path)]
        assert len(matches) == 1
        assert isinstance(matches[0], partial)
        assert matches[0].func is expected.func


def test_safe_fork_replaces_native_route():
    adapter = _make_adapter()
    routes = adapter._http_route_table()
    matches = [handler for method, path, handler in routes
               if (method, path) == ('POST', '/api/sessions/{session_id}/fork')]
    assert len(matches) == 1
    assert matches[0].__func__ is native.NativeSessionAPI._handle_fork_session


@pytest.mark.parametrize('kind', ['turn.started', 'assistant.delta', 'tool.started', 'tool.completed',
                                  'approval.request', 'clarify.request', 'turn.completed', 'turn.failed'])
def test_chat_sync_reads_committed_display_and_holds_until_delivery(monkeypatch, kind):
    adapter = _make_adapter()
    native.initialize(adapter)
    ref = 'native-ref'
    adapter._native_submit_ref_sessions[ref] = ('default', 'session-id')
    db = Mock()
    db.resolve_resume_session_id.return_value = 'compacted-id'
    db.get_messages.return_value = [{'role': 'user', 'content': 'hidden', 'display_kind': 'internal_notification'},
                                    {'role': 'assistant', 'content': 'saved', '_row_id': 12}]
    adapter._ensure_session_db = lambda: db
    adapter._release_native_awake = Mock()
    calls = []
    def publish(session_id, native_ref, event, read_display, secret, **kwargs):
        calls.append((session_id, native_ref, event, read_display(), secret('API_SERVER_KEY'), kwargs))
        return True
    monkeypatch.setattr(native, 'weave_chat_sync', SimpleNamespace(publish=publish), raising=False)
    monkeypatch.setattr(native, '_scoped_get_secret', lambda *args: 'test-secret')
    event = {'type': kind}
    adapter._native_submit_chat_sync(ref, event)
    assert len(calls) == 1
    session_id, native_ref, actual, rows, secret, callbacks = calls[0]
    assert (session_id, native_ref, actual, secret) == ('session-id', ref, event, 'test-secret')
    db.get_messages.assert_called_once_with('compacted-id', include_compacted=True, include_ancestors=True)
    assert rows[1]['_row_id'] == 12
    terminal = kind in {'turn.completed', 'turn.failed'}
    adapter._release_native_awake.assert_not_called()
    assert ('on_finished' in callbacks) == terminal
    if terminal:
        callbacks['on_finished']()
        adapter._release_native_awake.assert_called_once_with(ref)


def test_missing_writer_releases_terminal_hold(monkeypatch):
    adapter = _make_adapter()
    native.initialize(adapter)
    adapter._release_native_awake = Mock()
    monkeypatch.setattr(native, 'weave_chat_sync', None, raising=False)
    adapter._native_submit_chat_sync('unknown', {'type': 'turn.completed'})
    adapter._release_native_awake.assert_called_once_with('unknown')
