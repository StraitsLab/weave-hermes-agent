"""Weave (#830): the ``cron_run_credential`` plugin hook for native cron fires.

The first non-None answer (a SessionCredential) replaces the run's api_key; a
raising hook fails the run closed; with no plugin the runtime is untouched.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from agent.session_credential import SessionCredential
from cron.scheduler import run_job
from hermes_cli import plugins


JOB = {"id": "cred-job", "name": "cred", "prompt": "hello"}
RUNTIME = {
    "api_key": "no-key-required",
    "base_url": "https://harso.invalid/v1",
    "provider": "custom",
    "api_mode": "chat_completions",
}


@pytest.fixture
def hooks(monkeypatch):
    manager = plugins.PluginManager()
    manager._discovered = True
    monkeypatch.setattr(plugins, "_plugin_manager", manager, raising=False)
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "_delivery_manager", lambda: manager)
    return manager._hooks


def _run(tmp_path):
    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler._resolve_origin", return_value=None), \
         patch("dotenv.load_dotenv"), \
         patch("hermes_state.SessionDB", return_value=MagicMock()), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=dict(RUNTIME)), \
         patch("run_agent.AIAgent") as agent_cls:
        agent = MagicMock()
        agent.run_conversation.return_value = {"final_response": "ok"}
        agent_cls.return_value = agent
        success, _output, _final, error = run_job(dict(JOB))
    return success, error, agent_cls


def test_no_plugin_leaves_the_runtime_key_unchanged(tmp_path, hooks):
    success, error, agent_cls = _run(tmp_path)
    assert success is True and error is None
    assert agent_cls.call_args.kwargs["api_key"] == "no-key-required"


def test_hook_credential_reaches_the_agent(tmp_path, hooks):
    credential = SessionCredential("cron-bearer", datetime.now(timezone.utc) + timedelta(minutes=5))
    seen = []

    def narrow(job_id, session_id):
        seen.append(("narrow", job_id))
        return None

    def answer(job_id, session_id, provider, base_url):
        seen.append(("answer", job_id, session_id.startswith("cron_cred-job_"), provider, base_url))
        return credential

    hooks["cron_run_credential"] = [narrow, answer]
    success, error, agent_cls = _run(tmp_path)

    assert success is True and error is None
    kwargs = agent_cls.call_args.kwargs
    assert kwargs["api_key"] is credential
    assert kwargs["api_key"]() == "cron-bearer"
    assert kwargs["credential_pool"] is None
    assert seen == [
        ("narrow", "cred-job"),
        ("answer", "cred-job", True, "custom", "https://harso.invalid/v1"),
    ]


def test_raising_hook_fails_the_run_closed(tmp_path, hooks):
    def broken(**_kwargs):
        raise RuntimeError("ledger unreachable")

    hooks["cron_run_credential"] = [broken]
    success, error, agent_cls = _run(tmp_path)

    assert success is False
    assert "ledger unreachable" in error
    agent_cls.assert_not_called()


def test_non_credential_answer_fails_the_run_closed(tmp_path, hooks):
    hooks["cron_run_credential"] = [lambda **_kwargs: "raw-bearer-string"]
    success, error, agent_cls = _run(tmp_path)

    assert success is False
    assert "SessionCredential" in error
    agent_cls.assert_not_called()


def test_hook_is_a_valid_plugin_hook():
    assert "cron_run_credential" in plugins.VALID_HOOKS
