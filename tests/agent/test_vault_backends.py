"""Invariants for unlockable vault backends and the per-process unlock state.

Retargeted from upstream tests/agent/test_vault_backends.py @49b4286a22 (blob 60898cbe). The fork port
excludes the 1Password / Bitwarden backends, so the manager-CLI tests (bw ``--passwordenv`` contract,
1P/BW multi-origin binding, op config dir) are dropped with them. The backend-agnostic contracts stay,
driven through a test-local unlockable backend over the real ``agent.vault_backends.unlock`` module:

1. A locked backend never prompts where nobody can answer (cron/headless) and never leaks a value:
   browser_vault_list reports it under ``locked``, browser_vault_fill refuses.
2. A Lock acknowledged while an unlock is in flight wins; a session teardown releases only the tokens
   that session unlocked; tokens are scoped to the profile (HERMES_HOME).
"""

from __future__ import annotations

import json
import os
from typing import List, Optional
from unittest.mock import patch

import pytest

from agent.vault_backends import unlock as unlock_mod
from agent.vault_backends.base import LoginBackend, UnlockRequired
from agent.vault_store import VaultItemMeta

_PASSWORD = "plain sentence nobody would flag 7"


class _UnlockableBackend(LoginBackend):
    """Minimal external-manager stand-in: every call reads the real unlock module's token state."""

    name = "testmgr"
    display_name = "Test Manager"
    prefix = "tm:"
    needs_unlock = True

    def __init__(self):
        self.resolved: List[str] = []

    def is_unlocked(self) -> bool:
        return unlock_mod.is_unlocked(self.name)

    def unlock(self, master_password: str) -> None:
        generation = unlock_mod.begin_unlock(self.name)
        if master_password != "correct horse":
            raise RuntimeError("Invalid master password.")
        unlock_mod.store_session_token(self.name, "SESSION-TOKEN-123", generation)

    def _require(self) -> None:
        if unlock_mod.get_session_token(self.name) is None:
            raise UnlockRequired(self)

    def list_items(self) -> List[VaultItemMeta]:
        if not self.is_unlocked():
            return []
        return [VaultItemMeta(id="tm:abc", kind="login", label="Example", origin="https://example.com",
                              created_at="2026-01-01T00:00:00Z", identifier_type="email",
                              identifier="jane@example.com")]

    def get_meta(self, handle: str) -> Optional[VaultItemMeta]:
        self._require()
        return next((m for m in self.list_items() if m.id == handle), None)

    def resolve_password(self, handle: str, *, origin: Optional[str] = None) -> str:
        self._require()
        self.resolved.append(handle)
        return _PASSWORD


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    unlock_mod.lock()
    b = _UnlockableBackend()
    with patch("agent.vault_backends.base.enabled_backends", return_value=[b]), \
         patch("agent.vault_backends.enabled_backends", return_value=[b]):
        yield b
    unlock_mod.lock()
    unlock_mod.set_current_session_id(None)


def test_locked_manager_is_reported_not_prompted_when_headless(backend, monkeypatch):
    from tools.browser_vault_tool import browser_vault_fill, browser_vault_list

    monkeypatch.setenv("HERMES_CRON_SESSION", "1")  # headless: nobody can answer a prompt
    prompts = []
    unlock_mod.set_unlock_prompt_callback(lambda *a: prompts.append(a) or "correct horse")  # must not fire
    try:
        listed = json.loads(browser_vault_list())
        assert listed["items"] == []
        assert listed["locked"] == [{"backend": "testmgr", "display_name": "Test Manager",
                                     "unlock": "unavailable_in_this_session"}]
        filled = json.loads(browser_vault_fill("tm:abc", task_id="t"))
        assert filled["success"] is False and filled["error_type"] == "unlock_unavailable"
    finally:
        unlock_mod.set_unlock_prompt_callback(None)
    assert prompts == []
    assert backend.resolved == [], "no secret may be resolved while locked in a headless session"
    assert not unlock_mod.is_unlocked("testmgr")


def test_interactive_unlock_then_fill_routes_by_prefix_and_never_echoes_the_password(backend):
    from tools.browser_vault_tool import browser_vault_fill, browser_vault_list

    prompts = []

    def prompt(name, display):
        prompts.append((name, display))
        return "correct horse"

    unlock_mod.set_unlock_prompt_callback(prompt)
    try:
        with patch("agent.vault_backends.unlock.can_prompt_here", return_value=True), \
             patch("tools.browser_vault_tool._current_page_origin", return_value="https://example.com"), \
             patch("tools.browser_vault_tool._focus_bound_origin", return_value=None), \
             patch("tools.browser_vault_tool._eval_js", return_value={"success": True, "result": json.dumps([
                 {"tag": "input", "type": "password", "name": "password", "id": "pw", "autocomplete": "current-password",
                  "visible": True}])}), \
             patch("tools.browser_vault_tool._eval_js_secret", return_value={"success": True, "result": json.dumps(
                 {"filled": 1})}) as secret_eval:
            out = json.loads(browser_vault_fill("tm:abc", task_id="t"))
            out.pop("next")
            assert out == {"success": True, "filled_fields": 1, "backend": "testmgr", "kind": "login",
                           "origin": "https://example.com"}
            assert prompts == [("testmgr", "Test Manager")]
            listed = json.loads(browser_vault_list())
            assert listed["items"][0]["handle"] == "tm:abc"
            assert _PASSWORD not in json.dumps(listed) and _PASSWORD not in json.dumps(out)
            # The password reached the fill script, and only there.
            assert _PASSWORD in secret_eval.call_args.args[1]
    finally:
        unlock_mod.set_unlock_prompt_callback(None)
        from agent import redact
        redact.clear_vault_redaction_values()
    assert backend.resolved == ["tm:abc"]


def test_lock_during_unlock_wins_and_only_the_owning_session_release_drops_a_token(backend):
    # Lock races the in-flight unlock: the generation moved, so the late token is discarded.
    gen = unlock_mod.begin_unlock("testmgr")
    unlock_mod.lock("testmgr")
    assert unlock_mod.store_session_token("testmgr", "LATE-TOKEN", gen) is False
    assert not backend.is_unlocked()

    unlock_mod.set_current_session_id("sess-A")
    backend.unlock("correct horse")
    assert backend.is_unlocked()
    unlock_mod.release_session("sess-B")  # an unrelated sibling session ends
    assert backend.is_unlocked()
    unlock_mod.release_session("sess-A")
    assert not backend.is_unlocked()


def test_tokens_are_profile_scoped(backend, tmp_path):
    backend.unlock("correct horse")
    assert backend.is_unlocked()
    # Another HERMES_HOME sees the manager locked and cannot lock ours.
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path / "other-profile")}):
        assert not backend.is_unlocked()
        unlock_mod.lock("testmgr")
    assert backend.is_unlocked()
    unlock_mod.lock("testmgr")
    assert not backend.is_unlocked()
