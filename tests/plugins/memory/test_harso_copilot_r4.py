"""Round-4 ruling: the live copilot switch gates the FIRST composition of this turn's fetched memory.

An ON-origin prefetch result (structured delivery OR the legacy-item fallback) must not become newly model-visible
after the switch turned OFF, on both composition paths: the prologue's stamped ``api_content`` and the loop's
unstamped live composition. OFF-origin (default) legacy memory stays byte-identical to base. Throwaway HERMES_HOME
via the conftest.
"""
from __future__ import annotations

import types

import pytest

import agent.conversation_loop as conversation_loop
from agent.turn_context import build_turn_context, withhold_off_origin_memory
from tests.agent.test_api_content_sidecar import _chat_requests, _text_resp, _user_messages, wire_env  # noqa: F401
from tests.agent.test_turn_context import _FakeAgent, _build
from tests.plugins.memory.test_harso_copilot import _DELIVERY, _capture, _manager, _provider, _switch

_QUESTION = "What do you remember about my dentist appointment?"
_MEMORY_MARKERS = ("Harso memory delivery", "CACHE_SENTINEL")
_CASES = [(True, False), (True, True), (False, False), (False, True)]


def _response(representation):
    response = {"recall_status": "ok", "items": [{"citation": "[harso: e1]", "text": "CACHE_SENTINEL"}]}
    if representation == "delivery":
        response["delivery"] = _DELIVERY
    return response


def _flip_after_prefetch(monkeypatch, manager, end_on):
    """Edit config between the provider's completion and first composition (the real recall-indicator seam)."""
    original = manager.describe_recall

    def describe_then_flip():
        _switch(end_on)
        return original()

    monkeypatch.setattr(manager, "describe_recall", describe_then_flip)


def _has_memory(text):
    return any(marker in (text or "") for marker in _MEMORY_MARKERS)


@pytest.mark.parametrize("start_on,end_on", _CASES)
@pytest.mark.parametrize("representation", ["delivery", "items"])
def test_r4_stamped_first_composition_rechecks_live_switch(monkeypatch, representation, start_on, end_on):
    _switch(start_on)
    provider = _provider(monkeypatch)
    manager = _manager(provider)
    seen = _capture(monkeypatch, {"/context": _response(representation)})
    agent = _FakeAgent()
    agent._memory_manager = manager
    monkeypatch.setattr("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None)
    _flip_after_prefetch(monkeypatch, manager, end_on)

    ctx = _build(agent, user_message=_QUESTION)

    row = ctx.messages[ctx.current_turn_user_idx]
    assert len(seen) == 1 and provider.copilot_active() is end_on
    assert row["content"] == ctx.user_message == _QUESTION
    if start_on and not end_on:
        assert not _has_memory(row.get("api_content")), "ON-origin memory first composed after OFF"
        assert not _has_memory(ctx.ext_prefetch_cache), "the loop must not re-inject the withheld cache"
        assert ctx.ext_prefetch_gated is True
    else:
        assert _has_memory(row.get("api_content")), "stable ON / OFF-origin legacy memory must still render"


@pytest.mark.parametrize("start_on,end_on", _CASES)
@pytest.mark.parametrize("representation", ["delivery", "items"])
def test_r4_unstamped_first_composition_rechecks_live_switch(
    monkeypatch, wire_env, representation, start_on, end_on  # noqa: F811
):
    """The loop's live composition (prologue stamp skipped, as on MoA turns) is gated at first consumption too."""
    make_agent, handler, _db, _sid = wire_env
    _switch(start_on)
    provider = _provider(monkeypatch)
    manager = _manager(provider)
    seen = _capture(monkeypatch, {"/context": _response(representation)})
    _flip_after_prefetch(monkeypatch, manager, end_on)
    real_build = conversation_loop.build_turn_context
    stamped = []

    def build_without_stamp(*args, **kwargs):
        ctx = real_build(*args, **{**kwargs, "moa_active": True})
        stamped.append(ctx.ext_prefetch_gated)
        return ctx

    monkeypatch.setattr(conversation_loop, "build_turn_context", build_without_stamp)
    agent = make_agent()
    agent._memory_manager = manager
    handler.response_queue.append(_text_resp("done"))

    agent.run_conversation(_QUESTION, conversation_history=[], task_id="r4")

    sent = _user_messages(_chat_requests(handler)[0])[0]["content"]
    assert stamped == [False], "the prologue must not have gated the cache on this path"
    assert [r["path"] for r in seen][:1] == ["/internal/harso/context"]
    assert sent.startswith(_QUESTION) and "PLUGIN-CTX" in sent, "user and plugin text are kept"
    if start_on and not end_on:
        assert not _has_memory(sent), "ON-origin memory first composed after OFF on the unstamped path"
    else:
        assert _has_memory(sent), "stable ON / OFF-origin legacy memory must still render"


class _Probe:
    name = "harso"

    def __init__(self, origin, live):
        self._origin, self._live = origin, live

    def prefetch_copilot_origin(self):
        if isinstance(self._origin, Exception):
            raise self._origin
        return self._origin

    def copilot_active(self):
        if isinstance(self._live, Exception):
            raise self._live
        return self._live


def _agent_with(*providers):
    return types.SimpleNamespace(_memory_manager=types.SimpleNamespace(providers=list(providers)))


@pytest.mark.parametrize("origin,live,kept", [
    (True, True, True),                       # stable ON
    (False, False, True),                     # OFF origin: byte-identical pass-through
    (False, True, True),                      # OFF origin, switch turned ON later
    (True, False, False),                     # ON origin, switch now OFF: withheld
    (True, RuntimeError("config"), False),    # switch unreadable: fail safe
    (RuntimeError("origin"), True, False),    # origin unreadable: fail safe
])
def test_r4_withhold_helper_contract(origin, live, kept):
    cache = "[harso: e1] CACHE_SENTINEL"
    assert withhold_off_origin_memory(_agent_with(_Probe(origin, live)), cache) == (cache if kept else "")


def test_r4_withhold_helper_ignores_providers_without_origin_probe():
    cache = "## Other provider\nremembered"
    other = types.SimpleNamespace(name="other")
    assert withhold_off_origin_memory(_agent_with(other), cache) is cache
    assert withhold_off_origin_memory(types.SimpleNamespace(_memory_manager=None), cache) is cache
    assert withhold_off_origin_memory(_agent_with(_Probe(True, False)), "") == ""


def test_r4_origin_is_per_prefetch_and_covers_the_legacy_item_fallback(monkeypatch):
    _switch(True)
    provider = _provider(monkeypatch)
    _capture(monkeypatch, {"/context": _response("items")})
    out = provider.prefetch(_QUESTION)
    assert "CACHE_SENTINEL" in out and "Harso memory delivery" not in out  # no delivery marker to infer from
    assert provider.prefetch_copilot_origin() is True
    _switch(False)
    assert "CACHE_SENTINEL" in provider.prefetch(_QUESTION)
    assert provider.prefetch_copilot_origin() is False  # a new OFF prefetch replaces the previous ON origin
    assert provider.prefetch("   ") == "" and provider.prefetch_copilot_origin() is False
