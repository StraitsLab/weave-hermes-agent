"""User review of already-learned skills; learning itself stays unchanged."""
from aiohttp import web


def learned_metadata(skills):
    from tools.skill_usage import load_usage
    usage = load_usage()
    return [{**skill, "learned": usage.get(skill["name"], {}).get("created_by") == "agent",
             "kept": usage.get(skill["name"], {}).get("pinned") is True} for skill in skills]


async def review(request):
    from gateway.platforms.api_server import _error_response
    from tools import skill_usage
    from tools.skill_manager_tool import _validate_name
    try:
        body = await request.json()
    except ValueError:
        return _error_response("Invalid JSON", 400, code="invalid_learned_decision")
    if (not isinstance(body, dict) or set(body) != {"name", "decision"}
            or not isinstance(body["name"], str) or _validate_name(body["name"])
            or body["decision"] not in ("keep", "remove")):
        return _error_response("Invalid learned skill decision", 400, code="invalid_learned_decision")
    name = body["name"]
    # Native helpers permit opted-in bundled pruning and unmanaged local skills; this surface does not.
    record = skill_usage.load_usage().get(name, {})
    if (record.get("created_by") != "agent" or skill_usage.is_bundled(name)
            or not skill_usage.is_curation_eligible(name)):
        return _error_response("Only learned, locally managed skills can be reviewed", 403,
                               code="learned_skill_refused")
    from tools.skills_tool import _find_all_skills
    if not any(skill["name"] == name for skill in _find_all_skills(skip_disabled=False)):
        return _error_response("Learned skill not found", 404, code="learned_skill_not_found")
    success = (skill_usage.set_pinned(name, True) if body["decision"] == "keep"
               else skill_usage.archive_skill(name)[0])
    if not success:
        return _error_response("Skill review could not be saved", 409, code="learned_skill_refused")
    updated = skill_usage.load_usage().get(name, {})
    return web.json_response({"name": name, "learned": True, "kept": updated.get("pinned") is True,
                              "state": updated.get("state", "active")})
