"""Session credentials stay lazy through auxiliary resolution."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from agent import auxiliary_client as aux
from agent.session_credential import SessionCredential


@pytest.fixture
def runtime(monkeypatch, caplog):
    holder = SessionCredential("".join(("wvs_", "test_aux_private")), datetime.now(timezone.utc) + timedelta(days=1))
    base = "https://gate.example/v1"
    monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda task: {})
    monkeypatch.setattr(aux, "_scoped_key_env", lambda name: "")
    monkeypatch.setattr(aux, "_client_cache", {})
    token = aux.set_runtime_main("custom", "test-model", base_url=base, api_key=holder)
    try:
        yield holder, base
        assert holder() not in caplog.text + repr(holder)
    finally:
        aux.reset_runtime_main(token)


@pytest.mark.parametrize("route", ["main_key", "custom_runtime", "title", "named", "main_direct", "task_key", "named_other_host", "named_own_key"])
def test_callable_auxiliary_routes(runtime, monkeypatch, route):
    holder, base = runtime
    client = MagicMock()
    client.base_url = base
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(aux, "_create_openai_client", factory)
    if route == "main_key":
        assert aux._read_main_api_key() is holder
    elif route == "custom_runtime":
        monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda **kw: {
            "base_url": base, "api_key": holder, "api_mode": "chat_completions"})
        assert aux._resolve_custom_runtime() == (base, holder, "chat_completions")
    elif route == "task_key":
        monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda task: {"provider": "custom", "api_key": holder})
        assert aux._resolve_task_provider_model(task="title_generation")[3] is holder
    else:
        if route.startswith("named"):
            monkeypatch.setattr("hermes_cli.runtime_provider._get_named_custom_provider", lambda name: {
                "name": "weave-gate-b",
                "base_url": "https://other.example/v1" if route == "named_other_host" else base,
                "api_key": "own-static-key" if route == "named_own_key" else ""})
            aux.resolve_provider_client("custom:weave-gate-b", "test-model")
        elif route == "main_direct":
            aux.resolve_provider_client("custom", "test-model", main_runtime={
                "base_url": base, "api_key": holder})
        else:
            aux.call_llm(task="title_generation", messages=[{"role": "user", "content": "Title"}])
            client.chat.completions.create.assert_called_once()
        key = factory.call_args.kwargs["api_key"]
        if route == "named_other_host":
            assert key == "no-key-required"
        elif route == "named_own_key":
            assert key == "own-static-key"
        else:
            assert key is holder
