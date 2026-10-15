"""Harso's native session state, layered on the upstream split SessionDB."""
import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
import weakref
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from hermes_state_harso_schema import TRANSCRIPT_EPOCH_SQL
logger = logging.getLogger("hermes_state")


def strip_passive_metadata(message, display):
    if message.get("display_kind") == "passive_append" and not display:
        message.pop("display_kind", None)
        message.pop("display_metadata", None)


class SessionPassiveAppendError(ValueError):
    """Fail-closed error from the authenticated passive append primitive."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)

class _PassiveAppendGroupAborted(Exception):
    """Internal: roll back a passive append group that could not insert every item."""

    def __init__(self, result: dict):
        self.result = result
        super().__init__(result.get("outcome"))

class _CommitWakeHub:
    """Post-commit wake-ups for ONE state.db path (WEV-1817).

    Shared by every :class:`SessionDB` on the same file in this process (the
    gateway runner and the API adapter each hold their own instance). A wake
    carries no data: the database is the source of truth, and readers decide
    what changed from ``sessions.transcript_epoch`` and message ids.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._listeners: tuple = ()

    def add(self, listener: Callable[[], None]) -> None:
        with self._lock:
            self._listeners = self._listeners + (listener,)

    def remove(self, listener: Callable[[], None]) -> None:
        with self._lock:
            listeners = list(self._listeners)
            for index, existing in enumerate(listeners):
                if existing == listener:
                    del listeners[index]
                    self._listeners = tuple(listeners)
                    return

    def count(self) -> int:
        return len(self._listeners)

    def publish(self) -> None:
        for listener in self._listeners:  # immutable snapshot; no lock held
            try:
                listener()
            except Exception:
                logger.debug("commit wake listener failed", exc_info=True)

_commit_hubs_lock = threading.Lock()
_commit_hubs = weakref.WeakValueDictionary()
def _commit_hub_for(db_path) -> _CommitWakeHub:
    key = str(Path(db_path).resolve())
    with _commit_hubs_lock:
        hub = _commit_hubs.get(key)
        if hub is None:
            hub = _CommitWakeHub()
            _commit_hubs[key] = hub
        return hub

class HarsoSessionMixin:
    _TRANSCRIPT_READ_ATTEMPTS = 8

    def _bump_transcript_epoch(self, conn, session_id):
        # Even an empty active transcript is replaced by the compaction summary.
        # Fence its cursor inside the same transaction, without optional-column triggers.
        from hermes_state_harso_schema import _transcript_bump_sql
        advance, stamp, _ = _transcript_bump_sql("?").split(";")
        conn.execute(advance)
        conn.execute(stamp, (session_id,))

    def safe_fork_session(self, parent_session_id: str, child_session_id: str, *, before_commit=None) -> str:
        """Atomically publish or replay one explicit native fork."""
        def _do(conn):
            parent = conn.execute("SELECT * FROM sessions WHERE id = ?", (parent_session_id,)).fetchone()
            child = conn.execute("SELECT * FROM sessions WHERE id = ?", (child_session_id,)).fetchone()
            if child is not None:
                try:
                    config = json.loads(child["model_config"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    config = {}
                if not (
                    parent is not None
                    and child["parent_session_id"] == parent_session_id
                    and config.get("_branched_from") == parent_session_id
                    and child["ended_at"] is None
                    and parent["ended_at"] is not None and parent["end_reason"] == "branched"
                ):
                    return "conflict"
                if before_commit:
                    before_commit()
                return "identical_retry"
            if parent is None or parent["ended_at"] is not None:
                return "conflict"

            now = time.time()
            compression = conn.execute(
                "SELECT 1 FROM compression_locks WHERE session_id = ? AND expires_at > ? LIMIT 1",
                (parent_session_id, now),
            ).fetchone()
            conversation_id = self._session_turn_lease_key_on_conn(conn, parent_session_id)
            turn = conn.execute(
                "SELECT 1 FROM session_turn_leases WHERE conversation_id = ? AND expires_at > ? LIMIT 1",
                (conversation_id, now),
            ).fetchone()
            if compression is not None or turn is not None:
                return "boundary_unavailable"
            if before_commit:
                before_commit()

            config = json.loads(parent["model_config"] or "{}")
            if not isinstance(config, dict):
                config = {}
            config["_branched_from"] = parent_session_id
            conn.execute(
                """INSERT INTO sessions (
                   id, source, model, model_config, system_prompt, system_prompt_hash,
                   parent_session_id, cwd, git_branch, git_repo_root, profile_name,
                   user_id, session_key, chat_id, chat_type, thread_id, display_name,
                   origin_json, started_at, message_count, tool_call_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    child_session_id, parent["source"], parent["model"],
                    json.dumps(config), parent["system_prompt"],
                    parent["system_prompt_hash"], parent_session_id, parent["cwd"],
                    parent["git_branch"], parent["git_repo_root"],
                    parent["profile_name"], parent["user_id"], parent["session_key"],
                    parent["chat_id"], parent["chat_type"], parent["thread_id"],
                    parent["display_name"], parent["origin_json"], now,
                    parent["message_count"], parent["tool_call_count"],
                ),
            )
            columns = [name for name in self._message_column_names(conn) if name not in {"id", "session_id"}]
            names = ", ".join(columns)
            conn.execute(
                f"INSERT INTO messages (session_id, {names}) SELECT ?, {names} FROM messages "
                "WHERE session_id = ? AND active = 1 ORDER BY id",
                (child_session_id, parent_session_id),
            )
            updated = conn.execute(
                "UPDATE sessions SET ended_at = ?, end_reason = 'branched' WHERE id = ? AND ended_at IS NULL",
                (now, parent_session_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError("safe fork predecessor changed")
            return "forked"

        return self._execute_write(_do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S)
    def append_passive_messages(
        self,
        session_id: str,
        items: list[dict[str, Any]],
        *,
        predecessor_sequence: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Insert-or-return up to eight externally finalized messages as ONE contiguous group.

        Same per-item contract as :meth:`append_passive_message`, but every item
        is decided inside one native write transaction: either all items are new
        and are inserted back to back (no other writer can land between them),
        or all already exist identically (``identical_retry`` for each).  Any
        other mix answers ``idempotency_conflict`` for every item and writes
        nothing, so a partial group can never be created or extended.
        """
        if not isinstance(items, list) or not 1 <= len(items) <= 8:
            raise SessionPassiveAppendError("invalid_items", "items must hold 1..8 appends")
        if not all(isinstance(item, dict) for item in items):
            raise SessionPassiveAppendError("invalid_items", "every append must be an object")
        # Validate every item (string UUIDv7 ids included) before comparing ids, so a
        # malformed id is a schema error and never an unhashable-type crash.
        checked = [self._passive_append_item(session_id, predecessor_sequence=predecessor_sequence, **item)
                   for item in items]
        if len({item["external_item_id"] for item in checked}) != len(checked):
            raise SessionPassiveAppendError("invalid_items", "external_item_id values must be distinct")

        def _do(conn):
            outcomes = [self._passive_append_existing(conn, item) for item in checked]
            if all(outcome is not None for outcome in outcomes):
                if all(outcome["outcome"] == "identical_retry" for outcome in outcomes):
                    return outcomes
            if any(outcome is not None for outcome in outcomes):
                return [{"outcome": "idempotency_conflict", "message_id": None, "sequence": None}
                        for _ in checked]
            results = []
            for offset, item in enumerate(checked):
                # The tail after ``offset`` group inserts; an empty session's tail is None.
                base = predecessor_sequence or 0
                expected = (base + offset) or None
                result = self._passive_append_insert(conn, dict(item, predecessor_sequence=expected))
                if result["outcome"] != "inserted":
                    if results:
                        # Unreachable while the group holds the write lock; roll the txn back
                        # rather than ever commit a partial group.
                        raise _PassiveAppendGroupAborted(result)
                    return [result] + [dict(result) for _ in checked[1:]]
                results.append(result)
            return results

        try:
            return self._execute_write(_do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S)
        except _PassiveAppendGroupAborted as aborted:
            return [dict(aborted.result) for _ in checked]
    def append_passive_message(
        self,
        session_id: str,
        *,
        target_bem_session_id: str,
        external_item_id: str,
        role: str,
        content: str,
        canonical_sha256: str,
        participant_id: str,
        predecessor_sequence: Optional[int] = None,
    ) -> dict[str, Any]:
        """Insert-or-return one externally finalized message atomically.

        This is a deliberately narrow companion to :meth:`append_message` for
        passive callers that already own the finalized item.  Its dedicated
        global mapping is an idempotency index inside this SessionDB authority;
        the normal native message row remains the transcript authority and
        ordinary platform IDs remain independent.  Mapping, insert, and
        session-counter work share one native write transaction.

        The returned outcome is one of ``inserted``, ``identical_retry``,
        ``idempotency_conflict``, or ``sequence_gap``.  A missing target raises
        :class:`SessionPassiveAppendError` with ``session_not_found``.
        """
        item = self._passive_append_item(
            session_id,
            target_bem_session_id=target_bem_session_id,
            external_item_id=external_item_id,
            role=role,
            content=content,
            canonical_sha256=canonical_sha256,
            participant_id=participant_id,
            predecessor_sequence=predecessor_sequence,
        )

        def _do(conn):
            existing = self._passive_append_existing(conn, item)
            return existing if existing is not None else self._passive_append_insert(conn, item)

        return self._execute_write(_do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S)
    def _passive_append_item(
        self,
        session_id: str,
        *,
        target_bem_session_id: str,
        external_item_id: str,
        role: str,
        content: str,
        canonical_sha256: str,
        participant_id: str,
        predecessor_sequence: Optional[int] = None,
    ) -> dict[str, Any]:
        """Validate one passive append; return it normalized for the write transaction."""
        if not isinstance(session_id, str) or not session_id:
            raise SessionPassiveAppendError("invalid_session", "session_id is required")
        if not isinstance(target_bem_session_id, str) or not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            target_bem_session_id,
        ):
            raise SessionPassiveAppendError("invalid_bem_session_id", "target_bem_session_id must be a UUIDv7")
        if not isinstance(role, str) or role not in {"user", "assistant"}:
            raise SessionPassiveAppendError("invalid_role", "role must be user or assistant")
        if not isinstance(content, str) or not content:
            raise SessionPassiveAppendError("invalid_content", "content must be a non-empty string")
        try:
            content_bytes = content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise SessionPassiveAppendError("invalid_content", "content must be valid UTF-8") from exc
        if len(content_bytes) > 65_536:
            raise SessionPassiveAppendError("content_too_large", "content exceeds the 64 KiB limit")
        if not isinstance(external_item_id, str) or not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            external_item_id,
        ):
            raise SessionPassiveAppendError("invalid_external_item_id", "external_item_id must be a UUIDv7")
        if not isinstance(canonical_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", canonical_sha256
        ) or hashlib.sha256(content_bytes).hexdigest() != canonical_sha256:
            raise SessionPassiveAppendError("invalid_canonical_digest", "canonical_sha256 does not match content")
        if not isinstance(participant_id, str) or not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            participant_id,
        ):
            raise SessionPassiveAppendError("invalid_participant_id", "participant_id must be a UUIDv7")
        if predecessor_sequence is not None and (
            isinstance(predecessor_sequence, bool)
            or not isinstance(predecessor_sequence, int)
            or predecessor_sequence < 1
        ):
            raise SessionPassiveAppendError(
                "invalid_predecessor_sequence",
                "predecessor_sequence must be a positive integer",
            )

        stored_content = self._encode_content(content)

        return {
            "session_id": session_id,
            "target_bem_session_id": target_bem_session_id,
            "external_item_id": external_item_id,
            "role": role,
            "stored_content": stored_content,
            "canonical_sha256": canonical_sha256,
            "participant_id": participant_id,
            "predecessor_sequence": predecessor_sequence,
        }
    def _passive_append_existing(self, conn, item: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Inside the write txn: the stored outcome for a known item id, else None."""
        session_id = item["session_id"]
        target_bem_session_id = item["target_bem_session_id"]
        external_item_id = item["external_item_id"]
        role = item["role"]
        canonical_sha256 = item["canonical_sha256"]
        participant_id = item["participant_id"]
        conn.execute(
            """CREATE TABLE IF NOT EXISTS passive_append_idempotency (
               external_item_id TEXT PRIMARY KEY,
               session_id TEXT NOT NULL,
               target_bem_session_id TEXT NOT NULL,
               native_message_id INTEGER NOT NULL,
               canonical_sha256 TEXT NOT NULL,
               role TEXT NOT NULL,
               participant_id TEXT NOT NULL,
               predecessor_sequence INTEGER
            )"""
        )
        columns = {row["name"] for row in conn.execute(
            "PRAGMA table_info(passive_append_idempotency)"
        ).fetchall()}
        if "target_bem_session_id" not in columns:
            conn.execute("ALTER TABLE passive_append_idempotency ADD COLUMN target_bem_session_id TEXT")
            conn.execute("UPDATE passive_append_idempotency SET target_bem_session_id = session_id")
        existing = conn.execute(
            "SELECT external_item_id, session_id, target_bem_session_id, native_message_id, "
            "canonical_sha256, role, participant_id, predecessor_sequence "
            "FROM passive_append_idempotency WHERE external_item_id = ?",
            (external_item_id,),
        ).fetchone()
        if existing is not None:
            same_payload = (
                existing["session_id"] == session_id
                and existing["target_bem_session_id"] == target_bem_session_id
                and existing["canonical_sha256"] == canonical_sha256
                and existing["role"] == role
                and existing["participant_id"] == participant_id
            )
            if same_payload:
                return {
                    "outcome": "identical_retry",
                    "message_id": existing["native_message_id"],
                    "sequence": (existing["predecessor_sequence"] or 0) + 1,
                }
            return {
                "outcome": "idempotency_conflict",
                "message_id": None,
                "sequence": None,
            }

        return None
    def _passive_append_insert(self, conn, item: dict[str, Any]) -> dict[str, Any]:
        """Inside the write txn: insert one new item at its predecessor, or report the gap."""
        session_id = item["session_id"]
        target_bem_session_id = item["target_bem_session_id"]
        external_item_id = item["external_item_id"]
        role = item["role"]
        stored_content = item["stored_content"]
        canonical_sha256 = item["canonical_sha256"]
        participant_id = item["participant_id"]
        predecessor_sequence = item["predecessor_sequence"]
        session = conn.execute(
            "SELECT id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if session is None:
            raise SessionPassiveAppendError(
                "session_not_found", "target session was not found"
            )

        active_count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ? AND active = 1",
            (session_id,),
        ).fetchone()[0]
        expected_predecessor = active_count or None
        if predecessor_sequence != expected_predecessor:
            return {
                "outcome": "sequence_gap",
                "message_id": None,
                "sequence": expected_predecessor,
            }

        self._check_transcript_write_guards(conn, session_id, None)
        cursor = conn.execute(
            """INSERT INTO messages (
               session_id, role, content, timestamp, platform_message_id,
               observed, active, display_metadata, display_kind
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                role,
                stored_content,
                time.time(),
                None,
                1,
                1,
                self._encode_display_metadata({"participant_id": participant_id}),
                "passive_append",
            ),
        )
        message_id = cursor.lastrowid
        conn.execute(
            """INSERT INTO passive_append_idempotency (
               external_item_id, session_id, target_bem_session_id, native_message_id,
               canonical_sha256, role, participant_id, predecessor_sequence
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                external_item_id,
                session_id,
                target_bem_session_id,
                message_id,
                canonical_sha256,
                role,
                participant_id,
                predecessor_sequence,
            ),
        )
        conn.execute(
            "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
            (session_id,),
        )
        return {
            "outcome": "inserted",
            "message_id": message_id,
            "sequence": active_count + 1,
        }
    def _commit_hub(self) -> "_CommitWakeHub":
        hub = self.__dict__.get("_commit_wake_hub")
        if hub is None:
            hub = _commit_hub_for(self.db_path)
            self._commit_wake_hub = hub
        return hub
    def add_commit_listener(self, listener: Callable[[], None]) -> None:
        """Call *listener()* after every committed write to this state.db.

        Runs on the committing thread, outside the write lock. It must not
        block or raise: hand off (``loop.call_soon_threadsafe``) and return.
        Writes from another process on the same file do not wake it.
        """
        self._commit_hub().add(listener)
    def remove_commit_listener(self, listener: Callable[[], None]) -> None:
        self._commit_hub().remove(listener)
    def commit_listener_count(self) -> int:
        return self._commit_hub().count()
    def get_transcript_epoch(self, session_id: str) -> int:
        """Current transcript epoch of *session_id* (see TRANSCRIPT_TRIGGER_SQL)."""
        with self._read_ctx() as conn:
            row = conn.execute(TRANSCRIPT_EPOCH_SQL, (session_id,)).fetchone()
        return int(row[0] or 0) if row else 0
    def _message_session_id(self, message_id: int) -> Optional[str]:
        with self._read_ctx() as conn:
            row = conn.execute("SELECT session_id FROM messages WHERE id = ?", (message_id,)).fetchone()
        return row[0] if row else None
    def read_transcript_events(self, session_id: str, cursor: Optional[tuple[int, int]],
                               limit: int) -> dict[str, Any]:
        """One consistent page of the committed transcript after *cursor*.

        *cursor* is ``(epoch, id)`` or None (from the start). The page is
        consistent without a long read transaction: every non-append change
        bumps the epoch in its own commit, so the epoch is read before and
        after the resolution + row reads and the page is retried if it moved.
        Appends that land mid-read are safe (rows are keyed by id, and the
        head is read after the rows, so it never trails them).

        Returns ``{"session_id", "epoch", "head", "reset_required", "rows"}``
        where ``head`` is the max active id of the resolved session.
        """
        for _ in range(self._TRANSCRIPT_READ_ATTEMPTS):
            epoch = self.get_transcript_epoch(session_id)
            resolved = self.resolve_resume_session_id(session_id) or session_id
            reset = cursor is None or cursor[0] != epoch
            if not reset and cursor[1] > 0:
                # Resolution can move to another session through an append or
                # a heartbeat without an epoch bump (sibling ordering). The
                # client's rows then belong to a session it no longer shows.
                reset = self._message_session_id(cursor[1]) != resolved
            after_id = 0 if reset else cursor[1]
            rows = self.get_messages(resolved, limit=limit, after_id=after_id)
            head = self.get_active_message_watermark(resolved)
            if self.get_transcript_epoch(session_id) == epoch:
                return {"session_id": resolved, "epoch": epoch, "head": max(head, after_id),
                        "reset_required": reset, "rows": rows}
        raise sqlite3.OperationalError("transcript changed during every read attempt")
    def register_native_session_submit(
        self,
        session_id: str,
        *,
        external_request_id: str,
        message_sha256: str,
        native_request_ref: str,
    ) -> dict[str, Any]:
        """Atomically reserve-or-return one native gateway admission request.

        The record is deliberately only an idempotency index.  The gateway's
        existing session queue retains the message payload and remains the
        sole execution/transcript authority.
        """
        def _do(conn):
            conn.execute(
                """CREATE TABLE IF NOT EXISTS native_session_submit_idempotency (
                   external_request_id TEXT PRIMARY KEY,
                   session_id TEXT NOT NULL,
                   message_sha256 TEXT NOT NULL,
                   native_request_ref TEXT NOT NULL,
                   admission TEXT
                )"""
            )
            columns = {row["name"] for row in conn.execute(
                "PRAGMA table_info(native_session_submit_idempotency)"
            ).fetchall()}
            if "admission" not in columns:
                conn.execute("ALTER TABLE native_session_submit_idempotency ADD COLUMN admission TEXT")
            existing = conn.execute(
                "SELECT session_id, message_sha256, native_request_ref, admission "
                "FROM native_session_submit_idempotency WHERE external_request_id = ?",
                (external_request_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["session_id"] == session_id
                    and existing["message_sha256"] == message_sha256
                ):
                    return {"outcome": "identical_retry", "native_request_ref": existing["native_request_ref"], "admission": existing["admission"]}
                return {"outcome": "idempotency_conflict", "native_request_ref": None, "admission": None}
            if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone() is None:
                return {"outcome": "session_not_found", "native_request_ref": None, "admission": None}
            conn.execute(
                "INSERT INTO native_session_submit_idempotency "
                "(external_request_id, session_id, message_sha256, native_request_ref) "
                "VALUES (?, ?, ?, ?)",
                (external_request_id, session_id, message_sha256, native_request_ref),
            )
            return {"outcome": "inserted", "native_request_ref": native_request_ref, "admission": None}

        return self._execute_write(_do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S)
    def set_native_session_submit_admission(
        self, *, native_request_ref: str, admission: str
    ) -> None:
        """Persist the one native admission outcome for durable retries."""
        def _do(conn):
            conn.execute(
                "UPDATE native_session_submit_idempotency SET admission = ? "
                "WHERE native_request_ref = ? AND admission IS NULL",
                (admission, native_request_ref),
            )

        self._execute_write(_do)
    def remove_native_session_submit(
        self, *, external_request_id: str, native_request_ref: str
    ) -> None:
        """Release an unadmitted reservation after a native admission failure."""
        def _do(conn):
            conn.execute(
                "DELETE FROM native_session_submit_idempotency "
                "WHERE external_request_id = ? AND native_request_ref = ?",
                (external_request_id, native_request_ref),
            )

        self._execute_write(_do)
