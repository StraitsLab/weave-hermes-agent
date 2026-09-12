"""Behavior tests for the private Harso memory-provider boundary."""

from __future__ import annotations

import importlib
import inspect
import json
import logging
import urllib.error
from email.message import Message

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


def _turn_messages():
    return [
        {"role": "user", "_row_id": 11, "content": "Remember this"},
        {"role": "assistant", "_row_id": 12, "content": "Noted"},
    ]


def _capture_turn(monkeypatch, response=None):
    seen = []

    def open_request(request, timeout):
        seen.append({
            "url": request.full_url,
            "method": request.get_method(),
            "headers": dict(request.headers),
            "body": json.loads(request.data),
            "timeout": timeout,
        })
        return _Response(response or {"disposition": "admitted"})

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    return seen


def test_sync_turn_posts_exact_durable_pair_and_scope(monkeypatch):
    provider = _provider(monkeypatch)
    seen = _capture_turn(monkeypatch)
    history = [
        {"role": "user", "_row_id": 1, "content": "old question"},
        {"role": "assistant", "_row_id": 2, "content": "old answer"},
    ]
    provider.sync_turn("not authoritative", "not authoritative",
                       session_id="native-session", messages=history + _turn_messages())
    assert seen == [{
        "url": "https://memory.example.test/internal/harso/turns",
        "method": "POST",
        "headers": {
            "Content-type": "application/json",
            "Authorization": "Bearer cell-bearer",
            "X-weave-profile-route-key": "route-key",
        },
        "body": {
            "profile_id": "profile-1",
            "profile_revision_id": "revision-2",
            "hermes_session_ref": "native-session",
            "current_user_ref": "message:11",
            "current_assistant_ref": "message:12",
            "finalized_items": [
                {"role": "user", "native_item_ref": "message:11", "content": "Remember this"},
                {"role": "assistant", "native_item_ref": "message:12", "content": "Noted"},
            ],
        },
        "timeout": 5,
    }]


@pytest.mark.parametrize("messages", [
    None, [],
    [{"role": "user", "_row_id": 11, "content": "question"},
     {"role": "assistant", "content": "answer"}],
    [{"role": "assistant", "_row_id": 10, "content": "old answer"},
     {"role": "user", "_row_id": 11, "content": "question"}],
    [{"role": "user", "content": "question"},
     {"role": "assistant", "_row_id": 12, "content": "answer"}],
])
def test_sync_turn_skips_missing_durable_pair(monkeypatch, caplog, messages):
    provider = _provider(monkeypatch)
    seen = _capture_turn(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="plugins.memory.harso"):
        provider.sync_turn("question", "answer", messages=messages)
    assert seen == []
    assert "Harso turn skipped" in caplog.text


@pytest.mark.parametrize("row_id", [True, "12", 12.0, None])
def test_sync_turn_rejects_non_integer_refs(monkeypatch, row_id):
    provider = _provider(monkeypatch)
    seen = _capture_turn(monkeypatch)
    messages = _turn_messages()
    messages[-1]["_row_id"] = row_id
    provider.sync_turn("question", "answer", messages=messages)
    assert seen == []


@pytest.mark.parametrize("error", [
    urllib.error.HTTPError("https://memory.example.test", 500, "down", Message(), None),
    urllib.error.URLError("connection refused"),
])
def test_sync_turn_failure_warns_and_propagates_without_retry(monkeypatch, caplog, error):
    provider = _provider(monkeypatch)
    calls = []

    def open_request(request, timeout):
        calls.append(request.full_url)
        raise error

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    with pytest.raises(RuntimeError, match="harso_turn_unacknowledged"):
        provider.sync_turn("question", "answer", messages=_turn_messages())
    assert calls == ["https://memory.example.test/internal/harso/turns"]
    assert "Harso request unavailable" in caplog.text


def test_sync_turn_excluded_is_debug_not_error(monkeypatch, caplog):
    provider = _provider(monkeypatch)
    seen = _capture_turn(monkeypatch, {"disposition": "excluded"})
    with caplog.at_level(logging.DEBUG, logger="plugins.memory.harso"):
        provider.sync_turn("question", "answer", messages=_turn_messages())
    assert len(seen) == 1
    assert "excluded" in caplog.text
    assert all(record.levelno < logging.WARNING for record in caplog.records)


def test_sync_turn_ignores_tool_messages(monkeypatch):
    provider = _provider(monkeypatch)
    seen = _capture_turn(monkeypatch)
    messages = _turn_messages()
    tool = {"role": "tool", "_row_id": 99, "content": "tool output"}
    messages.insert(1, tool)
    messages.append(tool)
    provider.sync_turn("question", "answer", messages=messages)
    assert [item["native_item_ref"] for item in seen[0]["body"]["finalized_items"]] == [
        "message:11", "message:12",
    ]


def test_sync_turn_declares_messages_parameter(monkeypatch):
    provider = _provider(monkeypatch)
    assert "messages" in inspect.signature(provider.sync_turn).parameters


@pytest.mark.parametrize("role_index", [0, 1])
def test_sync_turn_skips_empty_content(monkeypatch, role_index):
    provider = _provider(monkeypatch)
    seen = _capture_turn(monkeypatch)
    messages = _turn_messages()
    messages[role_index]["content"] = " \n\t "
    provider.sync_turn("fallback user", "fallback assistant", messages=messages)
    assert seen == []


def test_sync_turn_flattens_multimodal_and_caps_text(monkeypatch):
    provider = _provider(monkeypatch)
    seen = _capture_turn(monkeypatch)
    messages = _turn_messages()
    messages[0]["content"] = [
        {"type": "text", "text": " first "},
        {"type": "image_url", "image_url": {"url": "ignored"}},
        {"type": "text", "text": "second"},
    ]
    messages[1]["content"] = "x" * 70000
    provider.sync_turn("question", "answer", messages=messages)
    items = seen[0]["body"]["finalized_items"]
    assert items[0]["content"] == "first \nsecond"
    assert items[1]["content"] == "x" * 65536


def test_sync_turn_skips_unavailable_provider(monkeypatch):
    provider = _provider(monkeypatch)
    provider._route_key = ""
    seen = _capture_turn(monkeypatch)
    provider.sync_turn("question", "answer", messages=_turn_messages())
    assert seen == []
