"""Forwarding middleware reports lifecycle without dispatching the local tool."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest


def setup(monkeypatch, callback):
    from agent import tool_executor as te, relay_tools
    from hermes_cli import middleware, plugins
    agent = SimpleNamespace(session_id='session', _tool_guardrails=SimpleNamespace(before_call=lambda *a: SimpleNamespace(allows_execution=True)))
    events = []
    monkeypatch.setattr(te, '_begin_tool_execution', lambda *a, **k: events.append(('start', k['tool_call_id'])))
    monkeypatch.setattr(relay_tools, 'execute', lambda name, args, next_call, **kw: (next_call(args), args))
    monkeypatch.setattr(middleware, 'apply_tool_request_middleware', lambda name, args, **kw: SimpleNamespace(payload=args, trace=[]))
    monkeypatch.setattr(middleware, '_get_middleware_callbacks', lambda kind: [callback])
    monkeypatch.setattr(plugins, '_dispatch_pre_tool_call_hooks', lambda *a, **kw: (None, None))
    execute = Mock(side_effect=lambda args: events.append(('local', args)) or 'local result')
    def run():
        return te._run_agent_tool_execution_middleware(agent, function_name='browser', function_args={'url':'https://example.org'}, effective_task_id='task', tool_call_id='native-call', execute=execute)
    return events, execute, run


def test_forwarded_start_precedes_remote_and_deduplicates(monkeypatch):
    def forward(args, next_call, **context):
        context['on_execution_started'](args)
        context['on_execution_started'](args)
        events.append(('remote', args))
        return 'remote result'
    events, execute, run = setup(monkeypatch, forward)
    assert run().result == 'remote result'
    assert events == [('start','native-call'),('remote',{'url':'https://example.org'})]
    execute.assert_not_called()


def test_refusal_does_not_claim_start_or_dispatch(monkeypatch):
    events, execute, run = setup(monkeypatch, lambda **kw: 'refused')
    assert run().result == 'refused'
    assert events == []
    execute.assert_not_called()


def test_start_observer_error_does_not_fall_through(monkeypatch):
    from agent import tool_executor as te
    def forward(args, next_call, **context):
        context['on_execution_started'](args)
        events.append(('remote', args))
        return 'remote result'
    events, execute, run = setup(monkeypatch, forward)
    monkeypatch.setattr(te, '_begin_tool_execution', Mock(side_effect=RuntimeError('observer failed')))
    assert run().result == 'remote result'
    assert events == [('remote', {'url':'https://example.org'})]
    execute.assert_not_called()


def test_passthrough_still_starts_once(monkeypatch):
    def forward(args, next_call, **context):
        return next_call(args)
    events, execute, run = setup(monkeypatch, forward)
    assert run().result == 'local result'
    assert events == [('start','native-call'),('local',{'url':'https://example.org'})]
    execute.assert_called_once()
