"""Session metadata update under the native credential lifecycle lock."""
import asyncio
from aiohttp import web
from gateway.platforms.api_server import _error_response

async def patch_session(self, request: web.Request) -> web.Response:
    """PATCH /api/sessions/{session_id} — update client-safe session metadata."""
    session_id = request.match_info["session_id"]
    session, err = await self._get_existing_session_or_404(session_id)
    if err:
        return err
    body, err = await self._read_json_body(request)
    if err:
        return err
    # pinned/archived/unread are durable desktop-sidebar flags.
    unknown = sorted(set(body) - {"title", "end_reason", "pinned", "archived", "hidden", "unread"})
    if unknown:
        return _error_response(
            f"Unsupported session fields: {', '.join(unknown)}", 400, code="unsupported_session_field")
    for flag in ("pinned", "archived", "hidden", "unread"):
        if flag in body and not isinstance(body[flag], bool):
            return _error_response(f"'{flag}' must be a boolean", 400, code="invalid_session_field")
    db = await self._ensure_session_db_async()
    if db is None:
        return self._session_db_unavailable()
    if "title" in body:
        try:
            await asyncio.to_thread(
                db.set_session_title, session_id, "" if body["title"] is None else str(body["title"]))
        except ValueError as exc:
            return _error_response(str(exc), 400, code="invalid_title")
    # Pinned last: set_session_pinned clears hidden, so a pin in the same request
    # wins over an explicit hidden (same order as the dashboard's _RENAME_FLAG_SETTERS).
    for flag, setter in (("archived", db.set_session_archived), ("hidden", db.set_session_hidden),
                         ("pinned", db.set_session_pinned)):
        if flag in body:
            await asyncio.to_thread(setter, session_id, body[flag])
    if "unread" in body:
        await asyncio.to_thread(db.set_session_read, session_id, read=not body["unread"])
    if body.get("end_reason"):
        from gateway.native_session_credentials import revoke
        revoke(self, session_id)
        await asyncio.to_thread(db.end_session, session_id, str(body["end_reason"]))
    session = await asyncio.to_thread(db.get_session, session_id) or session
    return web.json_response({"object": "hermes.session", "session": self._session_response(session)})
