"""Behavior tests for the private Harso memory-provider boundary."""

from __future__ import annotations

import importlib
import json


def _provider(monkeypatch):
    monkeypatch.setenv("WEAVE_HARSO_ENDPOINT", "https://memory.example.test")
    monkeypatch.setenv("WEAVE_HARSO_PROFILE_ID", "profile-1")
    monkeypatch.setenv("WEAVE_HARSO_PROFILE_REVISION_ID", "revision-2")
    monkeypatch.setenv("WEAVE_API_MCP_BEARER", "cell-bearer")
    monkeypatch.setenv("API_SERVER_KEY", "route-key")
    module = importlib.import_module("plugins.memory.harso")
    return importlib.reload(module).HarsoMemoryProvider()


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_prefetch_sends_native_session_and_exact_scope_headers(monkeypatch):
    provider = _provider(monkeypatch)
    seen = {}

    def open_request(request, timeout):
        seen["url"] = request.full_url
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.data)
        seen["timeout"] = timeout
        return _Response({
            "degraded": False,
            "items": [{"evidence_id": "evidence-9", "citation": "[harso: evidence-9]", "text": "remembered preference"}],
        })

    monkeypatch.setattr("urllib.request.urlopen", open_request)

    assert provider.prefetch("What did we decide?", session_id="native-session") == (
        "[harso: evidence-9] remembered preference"
    )
    assert seen == {
        "url": "https://memory.example.test/internal/harso/context",
        "headers": {
            "Content-type": "application/json",
            "Authorization": "Bearer cell-bearer",
            "X-weave-profile-route-key": "route-key",
        },
        "body": {
            "profile_id": "profile-1",
            "profile_revision_id": "revision-2",
            "hermes_session_ref": "native-session",
            "query": "What did we decide?",
        },
        "timeout": 5,
    }


def test_unavailable_prefetch_is_explicitly_empty_and_write_is_unacknowledged(monkeypatch):
    provider = _provider(monkeypatch)
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("down")))

    assert provider.prefetch("recall", session_id="native-session") == ""
    assert provider.on_memory_write(
        "replace", "memory", "accepted native text",
        metadata={"operation_id": "d4-operation", "revision": 7},
    ) is False


def test_write_uses_d4_operation_and_revision_without_synthesizing_ids(monkeypatch):
    provider = _provider(monkeypatch)
    seen = {}

    def open_request(request, timeout):
        seen["body"] = json.loads(request.data)
        return _Response({"acknowledged": True, "operation_id": "d4-operation", "revision": 7})

    monkeypatch.setattr("urllib.request.urlopen", open_request)

    assert provider.on_memory_write(
        "add", "memory", "native text",
        metadata={"operation_id": "d4-operation", "revision": 7},
    ) is True
    assert seen["body"] == {
        "profile_id": "profile-1",
        "profile_revision_id": "revision-2",
        "hermes_session_ref": "",
        "action": "add",
        "target": "memory",
        "content": "native text",
        "operation_id": "d4-operation",
        "revision": 7,
    }


def test_write_rejects_an_acknowledgement_for_a_different_d4_mutation(monkeypatch):
    provider = _provider(monkeypatch)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response({"acknowledged": True, "operation_id": "other", "revision": 7}),
    )

    assert provider.on_memory_write(
        "add", "memory", "native text",
        metadata={"operation_id": "d4-operation", "revision": 7},
    ) is False
