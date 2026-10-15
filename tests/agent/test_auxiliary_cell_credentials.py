"""Refreshable cell credentials must reach auxiliary clients uncalled."""
from unittest.mock import MagicMock

import pytest

from agent import auxiliary_client as aux


@pytest.fixture
def runtime(monkeypatch):
    holder = MagicMock(return_value="test-credential")
    base = "https://gate.example/v1"
    monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda task: {})
    monkeypatch.setattr(aux, "_scoped_key_env", lambda name: "")
    monkeypatch.setattr(aux, "_client_cache", {})
    token = aux.set_runtime_main("custom", "test-model", base_url=base, api_key=holder)
    try:
        yield holder, base
        holder.assert_not_called()
    finally:
        aux.reset_runtime_main(token)


@pytest.mark.parametrize("route", ["main_key", "custom_runtime", "task_key", "main_direct", "named", "named_other_origin", "named_own_key"])
def test_callable_auxiliary_routes(runtime, monkeypatch, route):
    holder, base = runtime
    factory = MagicMock()
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
                "name": "test-gate", "base_url": "https://gate.example:8443/v1" if route == "named_other_origin" else base,
                "api_key": "own-key" if route == "named_own_key" else ""})
            monkeypatch.setattr(aux, "load_pool", lambda *a: MagicMock(has_credentials=lambda: False))
            aux.resolve_provider_client("custom:test-gate", "test-model")
        else:
            aux.resolve_provider_client("custom", "test-model", main_runtime={"base_url": base, "api_key": holder})
        key = factory.call_args.kwargs["api_key"]
        if route == "named_other_origin":
            assert key == "no-key-required"
        elif route == "named_own_key":
            assert key == "own-key"
        else:
            assert key is holder
