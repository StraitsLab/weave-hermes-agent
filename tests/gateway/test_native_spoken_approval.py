"""DEC-415: a spoken "yes" on a call reaches the cell as a plain chat send. While the agent waits on an
approval, the gateway's own approval words answer it instead of queuing a turn behind it."""
from types import SimpleNamespace

from gateway.platforms.api_server_harso_sessions import NativeSessionAPI
from gateway.run_busy import GatewayBusySessionMixin


def _runner():
    return GatewayBusySessionMixin()  # the real word list, as the gateway runner uses it


def test_a_bare_yes_answers_the_blocking_approval(monkeypatch):
    resolved = []
    monkeypatch.setattr("tools.approval.has_blocking_approval", lambda key: key == "k1")
    monkeypatch.setattr("tools.approval.resolve_gateway_approval",
                        lambda key, choice, **kw: resolved.append((key, choice)) or 1)
    answer = NativeSessionAPI._native_submit_answers_approval
    assert answer(_runner(), "k1", "Yes.") is True
    assert answer(_runner(), "k1", "no") is True
    assert answer(_runner(), "k1", "yes but make it two") is False  # not a bare answer: a normal send
    assert answer(_runner(), "k2", "yes") is False  # nothing pending: a conversational yes
    assert resolved == [("k1", "once"), ("k1", "deny")]
