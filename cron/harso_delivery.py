"""Append a routine brief to its API conversation, never the push-send stub."""
from __future__ import annotations
import logging
import uuid

logger = logging.getLogger(__name__)
TRANSCRIPT_LEASE_WAIT_SECONDS = 30


def append_brief(chat_id, content, adapters=None):
    from gateway.config import Platform
    from hermes_state import SessionDB
    adapter = adapters.get(Platform.API_SERVER) if adapters else None
    db = getattr(adapter, "_session_db", None)
    own_db = db is None
    holder = "cron-delivery-" + uuid.uuid4().hex
    acquired = False
    try:
        if own_db:
            db = SessionDB()
        acquired = db.acquire_session_turn_lease(chat_id, holder, wait_seconds=TRANSCRIPT_LEASE_WAIT_SECONDS)
        if not acquired:
            raise RuntimeError("conversation is busy")
        session_id = db.resolve_resume_session_id(chat_id)
        if not session_id:
            raise RuntimeError("conversation is missing")
        row_id = db.append_message(session_id, role="assistant", content=content, turn_lease_holder=holder)
        _copy_to_app(db, chat_id, session_id, row_id)
        return None
    except Exception:
        logger.warning("cron transcript append failed", exc_info=True)
        return f"transcript append to api_server:{chat_id} failed"
    finally:
        if acquired:
            db.release_session_turn_lease(chat_id, holder)
        if own_db and db is not None:
            db.close()


def _copy_to_app(db, chat_id, session_id, row_id):
    """Weave: the app reads chat_items, not the cell's SQLite; copy the brief there as chat turns are (WEV-3451)."""
    try:
        from agent.secret_scope import get_secret, UnscopedSecretError
        from gateway import weave_chat_sync

        def secret(name):
            try:
                return get_secret(name, "") or ""
            except UnscopedSecretError:
                return ""

        row = [m for m in db.get_messages(session_id) if m.get("id") == row_id]
        if not weave_chat_sync.publish_rows(chat_id, row, secret):
            logger.warning("cron brief not copied to the app chat")
    except Exception:
        logger.warning("cron brief copy to the app chat failed", exc_info=True)
