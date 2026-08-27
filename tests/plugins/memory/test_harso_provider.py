"""Behavior tests for the private Harso memory-provider boundary."""

from __future__ import annotations

import importlib
import json

import pytest
from agent.memory_manager import MemoryManager


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
            "items": [
                {
                    "evidence_id": "evidence-9",
                    "citation": "[harso: evidence-9]",
                    "text": "remembered preference",
                }
            ],
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


def test_completed_turn_posts_only_user_authored_evidence(monkeypatch):
    provider = _provider(monkeypatch)
    seen = {}
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: (
            seen.update(url=request.full_url, body=json.loads(request.data), timeout=timeout)
            or _Response({"acknowledged": True, "disposition": "stored"})
        ),
    )

    provider.sync_turn(
        "I prefer tea",
        "I will remember that you prefer tea",
        session_id="weave-018f22e2-7c00-7001-8001-000000000001",
        messages=[{"role": "user"}, {"role": "assistant"}],
    )

    assert seen == {
        "url": "https://memory.example.test/internal/harso/turns",
        "body": {
            "profile_id": "profile-1",
            "profile_revision_id": "revision-2",
            "hermes_session_ref": "weave-018f22e2-7c00-7001-8001-000000000001",
            "user_content": "I prefer tea",
        },
        "timeout": 5,
    }


def test_completed_turn_payload_ignores_message_history_length(monkeypatch):
    provider = _provider(monkeypatch)
    bodies = []
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: (
            bodies.append(json.loads(request.data))
            or _Response({"acknowledged": True, "disposition": "stored"})
        ),
    )
    expected = {
        "profile_id": "profile-1",
        "profile_revision_id": "revision-2",
        "hermes_session_ref": "weave-session",
        "user_content": "I prefer tea",
    }

    for messages in (
        [{"role": "user"}, {"role": "assistant"}],
        [
            {"role": "user"},
            {"role": "assistant"},
            {"role": "tool"},
            {"role": "assistant"},
        ],
        None,
    ):
        provider.sync_turn(
            "  I prefer tea  ",
            "I will remember that you prefer tea",
            session_id="weave-session",
            messages=messages,
        )

    assert bodies == [expected, expected, expected]


def test_completed_turn_ignores_blank_user_content(monkeypatch):
    provider = _provider(monkeypatch)
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", calls.append)

    provider.sync_turn(" \n\t ", "ack", session_id="weave-session")

    assert calls == []


def test_completed_turn_is_fail_open(monkeypatch):
    provider = _provider(monkeypatch)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("down")),
    )
    assert provider.sync_turn("I prefer tea", "ack", session_id="weave-session") is None


def test_unavailable_prefetch_is_explicitly_empty_and_write_is_unacknowledged(
    monkeypatch,
):
    provider = _provider(monkeypatch)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("down")),
    )

    assert provider.prefetch("recall", session_id="native-session") == ""
    with pytest.raises(RuntimeError, match="harso_write_unacknowledged"):
        provider.on_memory_write(
            "replace",
            "memory",
            "accepted native text",
            metadata={"operation_id": "d4-operation", "revision": 7},
        )


def test_write_uses_d4_operation_and_revision_without_synthesizing_ids(monkeypatch):
    provider = _provider(monkeypatch)
    seen = {}

    def open_request(request, timeout):
        seen["body"] = json.loads(request.data)
        return _Response({
            "acknowledged": True,
            "operation_id": "d4-operation",
            "revision": 7,
        })

    monkeypatch.setattr("urllib.request.urlopen", open_request)

    assert (
        provider.on_memory_write(
            "add",
            "memory",
            "native text",
            metadata={"operation_id": "d4-operation", "revision": 7},
        )
        is True
    )
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
        lambda *_args, **_kwargs: _Response({
            "acknowledged": True,
            "operation_id": "other",
            "revision": 7,
        }),
    )

    with pytest.raises(RuntimeError, match="harso_write_unacknowledged"):
        provider.on_memory_write(
            "add",
            "memory",
            "native text",
            metadata={"operation_id": "d4-operation", "revision": 7},
        )


def test_native_mutation_acknowledges_matching_harso_receipt(monkeypatch):
    provider = _provider(monkeypatch)
    manager = MemoryManager()
    manager.add_provider(provider)
    writes = []
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response({
            "acknowledged": True,
            "operation_id": "d4-operation",
            "revision": 7,
        }),
    )

    result = manager.commit_native_mutation(
        "d4-operation",
        "add",
        "memory",
        "native text",
        lambda: writes.append("native") or {"success": True, "revision": 7},
    )

    assert result["success"] is True
    assert result["provider_acknowledged"] is True
    assert result["provider_status"] == "acknowledged"
    assert writes == ["native"]


@pytest.mark.parametrize(
    "response",
    [
        OSError("down"),
        {"acknowledged": False, "operation_id": "d4-operation", "revision": 7},
        {"acknowledged": True, "operation_id": "wrong-operation", "revision": 7},
    ],
)
def test_native_mutation_keeps_commit_but_reports_failed_harso_write_and_replays(
    monkeypatch, response
):
    provider = _provider(monkeypatch)
    manager = MemoryManager()
    manager.add_provider(provider)
    writes = []
    if isinstance(response, Exception):
        open_request = lambda *_args, **_kwargs: (_ for _ in ()).throw(response)
    else:
        open_request = lambda *_args, **_kwargs: _Response(response)
    monkeypatch.setattr("urllib.request.urlopen", open_request)

    result = manager.commit_native_mutation(
        "d4-operation",
        "add",
        "memory",
        "native text",
        lambda: writes.append("native") or {"success": True, "revision": 7},
    )
    replay = manager.commit_native_mutation(
        "d4-operation",
        "add",
        "memory",
        "native text",
        lambda: pytest.fail("D4 replay must not run the native writer"),
    )

    assert result["success"] is True
    assert result["provider_acknowledged"] is False
    assert result["provider_status"] == "failed"
    assert replay == result
    assert writes == ["native"]
