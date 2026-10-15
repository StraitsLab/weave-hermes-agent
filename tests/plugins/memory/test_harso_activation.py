from agent.harso_memory_ids import new_operation_id
"""Harso memory tests use the public plugin activation seam."""
from plugins.memory import load_memory_provider


def test_provider_loads_through_upstream_register_seam(monkeypatch):
    monkeypatch.setenv("WEAVE_HARSO_ENDPOINT", "https://memory.example.test")
    monkeypatch.setenv("WEAVE_API_MCP_BEARER", "test-only")
    monkeypatch.setenv("WEAVE_HARSO_PROFILE_ID", "profile-1")
    monkeypatch.setenv("WEAVE_HARSO_PROFILE_REVISION_ID", "revision-1")
    monkeypatch.setenv("API_SERVER_KEY", "test-route-only")
    provider = load_memory_provider("harso")
    assert provider is not None and provider.name == "harso"
    assert provider.is_available()


def test_service_mutation_vocabulary_and_correlated_ack(monkeypatch):
    import json
    import uuid
    from plugins.memory.harso import HarsoMemoryProvider
    provider = HarsoMemoryProvider()
    seen = []
    operation_id = new_operation_id()
    def post(path, body):
        seen.append((path, body))
        return {"acknowledged": True, "operation_id": body["operation_id"], "revision": body["revision"]}
    monkeypatch.setattr(provider, "_post", post)
    for action, expected, content in [("add", "assert", "tea"), ("patch", "correct", "coffee"), ("remove", "retract", "")]:
        assert provider.on_memory_write(action, "user", content, metadata={
            "operation_id": operation_id, "revision": 12, "previous_content": "coffee"})
        assert seen[-1][0] == "/internal/harso/mutations"
        assert seen[-1][1]["action"] == expected
        assert seen[-1][1]["target"] == "user"
        assert seen[-1][1]["content"] == (content or "coffee")
