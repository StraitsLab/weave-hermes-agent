"""Learned-skill review uses native pin/archive without changing learning policy."""
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from tools import skill_usage


@pytest.mark.asyncio
async def test_learned_review_pins_and_archives_recoverably(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    root = tmp_path / "skills"
    for name in ("learned", "bundled", "hub", "protected", "unmanaged"):
        folder = root / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: A skill\n---\nBody")
        if name != "unmanaged":
            skill_usage.mark_agent_created(name)
    (root / ".bundled_manifest").write_text("bundled:hash\n")
    (root / ".hub").mkdir()
    (root / ".hub" / "lock.json").write_text(json.dumps({"installed": {"hub": {}}}))
    monkeypatch.setattr(skill_usage, "PROTECTED_BUILTIN_SKILLS", {"protected"})
    # Even the opt-in to pruning built-ins must not let this user action archive them.
    monkeypatch.setattr(skill_usage, "_prune_builtins_enabled", lambda: True)
    external = tmp_path / "mounted" / "external"
    external.mkdir(parents=True)
    (external / "SKILL.md").write_text("---\nname: external\ndescription: External\n---\nBody")
    from agent import skill_utils
    monkeypatch.setattr(skill_utils, "get_all_skills_dirs", lambda: [root, external.parent])
    usage = skill_usage.load_usage()
    usage["external"] = {"created_by": "agent"}
    (root / ".usage.json").write_text(json.dumps(usage))
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-key"}))
    app = web.Application()
    app.router.add_get("/v1/skills", adapter._handle_skills)
    app.router.add_post("/v1/skills/learned", adapter._handle_learned_skills)
    async with TestClient(TestServer(app)) as client:
        headers = {"Authorization": "Bearer test-key"}
        assert (await client.post("/v1/skills/learned", json={"name": "learned", "decision": "keep"})).status == 401
        listed = await (await client.get("/v1/skills", headers=headers)).json()
        learned = next(row for row in listed["data"] if row["name"] == "learned")
        assert (learned["learned"], learned["kept"]) == (True, False)
        response = await client.post("/v1/skills/learned", headers=headers, json={"name": "learned", "decision": "keep"})
        assert response.status == 200
        assert (await response.json())["kept"] is True
        assert skill_usage.load_usage()["learned"]["pinned"] is True
        from agent.curator import apply_automatic_transitions
        from datetime import UTC, datetime
        apply_automatic_transitions(now=datetime(2100, 1, 1, tzinfo=UTC))
        assert skill_usage.load_usage()["learned"]["state"] == "active"
        for name in ("bundled", "hub", "protected", "unmanaged", "external", "unknown", "../x"):
            for decision in ("keep", "remove"):
                response = await client.post("/v1/skills/learned", headers=headers, json={"name": name, "decision": decision})
                assert response.status in (400, 403, 404)
                assert (await response.json())["error"]["code"]
        for body in ({"name": "learned", "decision": "delete"}, {"name": "learned", "decision": "keep", "path": "x"}):
            assert (await client.post("/v1/skills/learned", headers=headers, json=body)).status == 400
        response = await client.post("/v1/skills/learned", headers=headers, json={"name": "learned", "decision": "remove"})
        assert response.status == 200
        assert (await response.json())["state"] == "archived"
        assert not (root / "learned").exists()
        assert (root / ".archive" / "learned" / "SKILL.md").read_text().endswith("Body")
        assert skill_usage.restore_skill("learned")[0]
        assert (external / "SKILL.md").read_text().endswith("Body")
