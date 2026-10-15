"""Harso native session HTTP contract; execution uses upstream session admission."""
import asyncio
from agent.secret_scope import get_secret as _scoped_get_secret, UnscopedSecretError as _UnscopedSecretError
try:
    from gateway import weave_chat_sync
except ImportError:
    weave_chat_sync = None
import hashlib
import inspect
import json
import logging
import re
import threading
import uuid
from contextlib import suppress, asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple
from aiohttp import web
from gateway.config import Platform
from gateway.session import SessionSource
from gateway.platforms.base import MessageEvent, MessageType, SendResult
from gateway.platforms.api_server import (_openai_error, _api_request_profile,
    _sse_frame, CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS, redact_sensitive_text, _project_client_message)
_NATIVE_SUBMIT_BUSY_MODES = {"queue", "steer", "interrupt"}
logger = logging.getLogger("gateway.platforms.api_server")

class NativeSessionAPI:
    _EVENT_CURSOR_RE = re.compile(r"^([0-9]+)\.([0-9]+)$")
    _NATIVE_APPROVAL_CHOICES = ("once", "deny")






    @classmethod
    def _parse_event_cursor(cls, raw: Optional[str]):
        """``(epoch, id)``, ``None`` for from-the-start, or ``False`` if malformed."""
        if raw is None or raw == "" or raw == "0":
            return None
        match = cls._EVENT_CURSOR_RE.fullmatch(raw)
        if match is None:
            return False
        return int(match.group(1)), int(match.group(2))

    @staticmethod
    def _event_cursor(epoch: int, message_id: int) -> str:
        return f"{int(epoch)}.{int(message_id)}"

    def _session_event_item(self, epoch: int, row: dict[str, Any]) -> dict[str, Any]:
        return {"cursor": self._event_cursor(epoch, row["id"]), "message": self._message_response(row)}

    async def _handle_session_events(self, request: "web.Request") -> "web.Response":
        """GET /api/sessions/{session_id}/events?after=<cursor>&limit=M — committed items after a cursor."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        _, err = await self._get_existing_session_or_404(session_id)
        if err:
            return err
        cursor = self._parse_event_cursor(request.query.get("after"))
        raw_limit = request.query.get("limit")
        try:
            limit = 200 if raw_limit is None else int(raw_limit)
        except (TypeError, ValueError):
            limit = -1
        if cursor is False or not 1 <= limit <= 500:
            return web.json_response(
                _openai_error(
                    'after must be "<epoch>.<id>" or 0, and limit an integer in 1..500',
                    code="invalid_pagination",
                ),
                status=400,
            )
        db = await self._ensure_session_db_async()
        page = await asyncio.to_thread(db.read_transcript_events, session_id, cursor, limit)
        epoch = page["epoch"]
        return web.json_response({
            "object": "conversation_events",
            "session_id": page["session_id"],
            "epoch": epoch,
            "head": self._event_cursor(epoch, page["head"]),
            "reset_required": page["reset_required"],
            "items": [self._session_event_item(epoch, row) for row in page["rows"]],
        })

    async def _handle_session_append(self, request: "web.Request") -> "web.Response":
        """POST /api/sessions/{session_id}/append — passive native append."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        body, err = await self._read_json_body(request)
        if err:
            return err

        if set(body) != {"request", "content"} or not isinstance(body.get("request"), dict):
            return web.json_response(
                _openai_error("Invalid append schema", code="invalid_append_schema"),
                status=400,
            )
        append_request = body["request"]
        content = body["content"]
        required = {
            "kind", "authenticated", "external_item_id", "external_identity_scope",
            "transcript_row", "atomic_insert_or_return", "target_bem_session_id",
            "role", "canonical_sha256", "participant_id",
        }
        if (
            set(append_request) - required - {"predecessor_sequence"}
            or not required <= set(append_request)
            or not isinstance(content, str)
        ):
            return web.json_response(
                _openai_error("Invalid append schema", code="invalid_append_schema"),
                status=400,
            )
        try:
            content.encode("utf-8")
        except UnicodeEncodeError:
            return web.json_response(
                _openai_error("Invalid append content", code="invalid_content"), status=400
            )
        if len(content.encode("utf-8")) > 65_536:
            return web.json_response(
                _openai_error("Append content exceeds the 64 KiB limit", code="content_too_large"),
                status=400,
            )
        def uuid7(value):
            return isinstance(value, str) and bool(re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", value
            ))
        if (
            append_request["kind"] != "hermes_append_request"
            or append_request["authenticated"] is not True
            or append_request["external_identity_scope"] != "global"
            or append_request["transcript_row"] != "normal"
            or append_request["atomic_insert_or_return"] is not True
            or not uuid7(append_request["external_item_id"])
            or not uuid7(append_request["target_bem_session_id"])
            or not isinstance(append_request["role"], str)
            or append_request["role"] not in {"user", "assistant"}
            or not uuid7(append_request["participant_id"])
            or not isinstance(append_request["canonical_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", append_request["canonical_sha256"])
            or hashlib.sha256(content.encode("utf-8")).hexdigest()
            != append_request["canonical_sha256"]
        ):
            return web.json_response(
                _openai_error("Invalid append schema", code="invalid_append_schema"),
                status=400,
            )
        predecessor = append_request.get("predecessor_sequence")
        if predecessor is not None and (
            isinstance(predecessor, bool) or not isinstance(predecessor, int) or predecessor < 1
        ):
            return web.json_response(
                _openai_error("Invalid append schema", code="invalid_append_schema"),
                status=400,
            )

        db = await self._ensure_session_db_async()
        if db is None:
            return web.json_response(
                _openai_error("Session database unavailable", code="session_db_unavailable"),
                status=503,
            )
        try:
            result = await asyncio.to_thread(
                db.append_passive_message,
                session_id,
                target_bem_session_id=append_request["target_bem_session_id"],
                external_item_id=append_request["external_item_id"],
                role=append_request["role"],
                content=content,
                canonical_sha256=append_request["canonical_sha256"],
                participant_id=append_request["participant_id"],
                predecessor_sequence=predecessor,
            )
        except Exception as exc:  # health: allow BLE001,S110 -- HTTP/credential boundary: return typed refusal without exception contents.
            from hermes_state import SessionPassiveAppendError

            if isinstance(exc, SessionPassiveAppendError):
                if exc.code == "session_not_found":
                    return web.json_response(
                        _openai_error("Target session was not found", code="session_not_found"),
                        status=404,
                    )
                return web.json_response(
                    _openai_error("Invalid append schema", code=exc.code),
                    status=400,
                )
            logger.warning("[%s] passive session append failed: %s", self.name, type(exc).__name__)
            return web.json_response(
                _openai_error("Session append unavailable", code="session_append_unavailable"),
                status=503,
            )

        outcome = result["outcome"]
        external_item_id = append_request["external_item_id"]
        receipt = {
            "kind": "hermes_append_receipt",
            "receipt_id": external_item_id,
            "external_item_id": external_item_id,
            "outcome": outcome,
            "terminal": outcome != "sequence_gap",
            "retryable": outcome == "sequence_gap",
            "ledger_recording": "idempotent",
            "cross_service_2pc": False,
        }
        if outcome in {"inserted", "identical_retry"}:
            receipt["native_item_ref"] = f"message:{result['message_id']}"
            if outcome == "identical_retry":
                receipt["same_native_ref_on_identical_retry"] = True
            return web.json_response(receipt, status=201 if outcome == "inserted" else 200)
        if outcome == "sequence_gap":
            return web.json_response(
                receipt,
                status=409,
            )
        return web.json_response(
            receipt,
            status=409,
        )

    async def _handle_session_append_group(self, request: "web.Request") -> "web.Response":
        """POST /api/sessions/{session_id}/append/group — 1..8 passive appends committed contiguously.

        Body: {"requests": [<hermes_append_request>...], "contents": [str...], "predecessor_sequence"?: int}.
        Each request has the exact /append schema minus predecessor_sequence; the group shares one.
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        body, err = await self._read_json_body(request)
        if err:
            return err
        requests_, contents = body.get("requests"), body.get("contents")
        predecessor = body.get("predecessor_sequence")
        invalid = web.json_response(_openai_error("Invalid append schema", code="invalid_append_schema"), status=400)
        required = {
            "kind", "authenticated", "external_item_id", "external_identity_scope",
            "transcript_row", "atomic_insert_or_return", "target_bem_session_id",
            "role", "canonical_sha256", "participant_id",
        }
        if (
            set(body) - {"requests", "contents", "predecessor_sequence"}
            or not isinstance(requests_, list) or not isinstance(contents, list)
            or not 1 <= len(requests_) <= 8 or len(requests_) != len(contents)
            or (predecessor is not None and (isinstance(predecessor, bool) or not isinstance(predecessor, int) or predecessor < 1))
        ):
            return invalid
        items = []
        for append_request, content in zip(requests_, contents):
            if (
                not isinstance(append_request, dict) or set(append_request) != required
                or not isinstance(content, str)
                or append_request["kind"] != "hermes_append_request"
                or append_request["authenticated"] is not True
                or append_request["external_identity_scope"] != "global"
                or append_request["transcript_row"] != "normal"
                or append_request["atomic_insert_or_return"] is not True
            ):
                return invalid
            # Per-field rules (UUIDv7 ids, role, digest, UTF-8, 64 KiB) are enforced
            # once, by SessionDB._passive_append_item, and surface below as 400.
            items.append({
                "target_bem_session_id": append_request["target_bem_session_id"],
                "external_item_id": append_request["external_item_id"],
                "role": append_request["role"],
                "content": content,
                "canonical_sha256": append_request["canonical_sha256"],
                "participant_id": append_request["participant_id"],
            })
        db = await self._ensure_session_db_async()
        if db is None:
            return web.json_response(
                _openai_error("Session database unavailable", code="session_db_unavailable"), status=503)
        try:
            results = await asyncio.to_thread(
                db.append_passive_messages, session_id, items, predecessor_sequence=predecessor)
        except Exception as exc:  # health: allow BLE001,S110 -- HTTP/credential boundary: return typed refusal without exception contents.
            from hermes_state import SessionPassiveAppendError

            if isinstance(exc, SessionPassiveAppendError):
                if exc.code == "session_not_found":
                    return web.json_response(
                        _openai_error("Target session was not found", code="session_not_found"), status=404)
                return web.json_response(_openai_error("Invalid append schema", code=exc.code), status=400)
            logger.warning("[%s] passive group append failed: %s", self.name, type(exc).__name__)
            return web.json_response(
                _openai_error("Session append unavailable", code="session_append_unavailable"), status=503)
        receipts = []
        for item, result in zip(items, results):
            outcome = result["outcome"]
            receipt = {
                "kind": "hermes_append_receipt",
                "receipt_id": item["external_item_id"],
                "external_item_id": item["external_item_id"],
                "outcome": outcome,
                "terminal": outcome != "sequence_gap",
                "retryable": outcome == "sequence_gap",
                "ledger_recording": "idempotent",
                "cross_service_2pc": False,
            }
            if outcome in {"inserted", "identical_retry"}:
                receipt["native_item_ref"] = f"message:{result['message_id']}"
                if outcome == "identical_retry":
                    receipt["same_native_ref_on_identical_retry"] = True
            receipts.append(receipt)
        outcomes = {result["outcome"] for result in results}
        status = 201 if outcomes == {"inserted"} else 200 if outcomes == {"identical_retry"} else 409
        return web.json_response({"kind": "hermes_append_group_receipt", "receipts": receipts}, status=status)

    async def _handle_session_credential_bind(self, request: "web.Request") -> "web.Response":
        """Bind one controller-supplied bearer to an existing live session."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        body, err = await self._read_json_body(request)
        if err or set(body) != {
            "credential_slot", "bearer", "expires_at", "provider_route_revision_id",
        }:
            return web.json_response(
                _openai_error("Invalid session credential schema", code="invalid_session_credential_schema"),
                status=400,
            )
        from agent.session_credential import _CREDENTIAL_EXPIRY_RE, parse_credential_expiry

        bearer = body["bearer"]
        revision = body["provider_route_revision_id"]
        expires_at = parse_credential_expiry(body["expires_at"])
        if expires_at is None and isinstance(body["expires_at"], str) and _CREDENTIAL_EXPIRY_RE.fullmatch(body["expires_at"]):
            try:
                from datetime import datetime
                datetime.fromisoformat(body["expires_at"])
            except ValueError:
                pass
            else:
                return web.json_response(_openai_error("Credential unavailable", code="credential_unavailable"), status=409)
        if (
            body["credential_slot"] != "GATE_B_API_KEY"
            or not isinstance(bearer, str) or not bearer.strip()
            or not isinstance(revision, str) or not revision.strip()
            or expires_at is None
        ):
            return web.json_response(
                _openai_error("Invalid session credential schema", code="invalid_session_credential_schema"),
                status=400,
            )
        runner = self.gateway_runner
        if runner is None or not getattr(runner, "_running", False):
            return web.json_response(
                _openai_error("Native gateway unavailable", code="native_gateway_unavailable"), status=503
            )
        session_id = request.match_info["session_id"]
        _, session_err = await self._get_existing_session_or_404(session_id)
        if session_err:
            return session_err
        from gateway.native_bot_admission import enabled as native_bot_sessions
        if native_bot_sessions():
            # Native profiles resolve their own short-lived key_cmd credential.
            # Do not bind an unused controller bearer to the legacy runner or
            # resurrect its ended-session record during a rolling API update.
            return web.json_response({"status": "ready", "credential_slot": "GATE_B_API_KEY"})
        source = SessionSource(
            platform=Platform.API_SERVER, chat_id=session_id, chat_type="dm",
            user_id="api_server", user_name="API server",
            profile=_api_request_profile.get() or None,
        )
        async with self._native_lifecycle_lock(session_id):
            from gateway.session import SessionResetRequired

            try:
                bound = await asyncio.to_thread(
                    runner.bind_session_credential, source, session_id, bearer, expires_at, revision,
                    raise_on_reset=True,
                )
            except SessionResetRequired as exc:
                return self._session_reset_required(exc.cause)
        if not bound:
            return web.json_response(
                _openai_error("Credential unavailable", code="credential_unavailable"), status=409
            )
        return web.json_response({"status": "ready", "credential_slot": "GATE_B_API_KEY"})

    @staticmethod
    def _session_reset_required(cause: str) -> "web.Response":
        # Fixed tokens only: neither bearer values nor descendant identities.
        logger.warning("native_session_reset_required cause=%s", cause)
        return web.json_response(
            _openai_error("Session reset required", code="session_reset_required"), status=409,
        )

    @staticmethod
    def _native_submit_request(body: dict[str, Any]) -> tuple[Optional[tuple[str, str, str, Optional[str], bool]], Optional["web.Response"]]:
        """Validate the deliberately narrow native-admission request shape."""
        if set(body) - {"expected_active_ref", "internal", "hub_tools"} != {"kind", "external_request_id", "message", "busy_mode"}:
            return None, web.json_response(
                _openai_error("Invalid native submit schema", code="invalid_native_submit_schema"), status=400
            )
        from gateway.hub_revision import validate
        if "hub_tools" in body and not validate(body["hub_tools"]):
            return None, web.json_response(_openai_error("Invalid hub revision", code="invalid_native_submit_schema"), status=400)
        request_id = body.get("external_request_id")
        message = body.get("message")
        if (
            body.get("kind") != "hermes.session.submit"
            or not isinstance(body.get("internal", False), bool)
            or not isinstance(body.get("busy_mode"), str)
            or body["busy_mode"] not in _NATIVE_SUBMIT_BUSY_MODES
            or ("expected_active_ref" in body and (not isinstance(body["expected_active_ref"], str) or not body["expected_active_ref"]))
            or not isinstance(request_id, str)
            or not re.fullmatch(r"[\x21-\x7e]{1,128}", request_id)
            or not isinstance(message, str)
            or not message.strip()
        ):
            return None, web.json_response(
                _openai_error("Invalid native submit schema", code="invalid_native_submit_schema"), status=400
            )
        try:
            if len(message.encode("utf-8")) > 65_536:
                raise ValueError
        except (UnicodeEncodeError, ValueError):
            return None, web.json_response(
                _openai_error("Invalid native submit schema", code="invalid_native_submit_schema"), status=400
            )
        return (request_id, message, body["busy_mode"], body.get("expected_active_ref"), body.get("internal", False)), None

    def _record_native_submit_handoff(self, native_request_ref: str, admission: Any) -> None:
        self._native_submit_handoffs[native_request_ref] = admission
        if len(self._native_submit_handoffs) > 1_024:
            self._native_submit_handoffs.pop(next(iter(self._native_submit_handoffs)), None)

    async def _wait_native_submit_start(
        self, native_request_ref: str, started: "asyncio.Future[str]", *, retry: bool = False,
    ) -> str:
        try:
            return await asyncio.wait_for(
                asyncio.shield(started), timeout=self._native_submit_start_timeout_s,
            )
        except Exception:  # health: allow BLE001,S110 -- HTTP/credential boundary: return typed refusal without exception contents.
            # A failed receipt write does not undo a start. Only recover a failed
            # future on retry with observed lifecycle evidence, never a timeout.
            if (retry and started.done() and not started.cancelled()
                    and started.exception() is not None
                    and (native_request_ref in self._native_submit_active_refs.values()
                         or native_request_ref in self._native_submit_terminals)):
                return "streaming"
            raise

    async def _admit_native_session_submit(
        self, session_id: str, message: str, native_request_ref: str,
        external_request_id: str = "", busy_mode: str = "queue", expected_active_ref: Optional[str] = None,
        internal: bool = False,
    ) -> str:
        """Submit one user or internal turn through the running gateway's writer lease."""
        from gateway.native_bot_admission import enabled, admit
        from gateway.session_authorities import active_authority
        if enabled() or active_authority(self.gateway_runner) is not None:
            return await admit(self, _api_request_profile.get() or "default", session_id,
                               message, native_request_ref, external_request_id, internal=internal,
                               busy_mode=busy_mode, expected_active_ref=expected_active_ref)
        runner = self.gateway_runner
        if (
            runner is None
            or not getattr(runner, "_running", False)
            or getattr(runner, "_draining", False)
            or getattr(runner, "_startup_restore_in_progress", False)
        ):
            raise RuntimeError("native gateway runner is unavailable")

        # D-119: an internal notification is always its own turn, never a steer
        # or interrupt of a user turn. Queue mode also ignores the active-ref fence.
        if internal:
            busy_mode = "queue"

        existing_start = self._native_submit_started.get(native_request_ref)
        if existing_start is not None:
            return await self._wait_native_submit_start(native_request_ref, existing_start, retry=True)

        source = SessionSource(
            platform=Platform.API_SERVER,
            chat_id=session_id,
            chat_type="dm",
            user_id="api_server",
            user_name="API server",
            profile=_api_request_profile.get() or None,
        )
        entry = await runner.async_session_store.bind_existing_session(
            source, session_id, reopen=False
        )
        if entry is None or entry.session_id != session_id:
            raise RuntimeError("native session binding was rejected")
        self._native_submit_ref_sessions[native_request_ref] = (
            _api_request_profile.get() or "default", session_id,
        )
        event = MessageEvent(
            text=message,
            message_type=MessageType.TEXT,
            user_id=source.user_id,
            user_name=source.user_name,
            source=source,
            message_id=native_request_ref,
            internal=internal,
            allow_gateway_control=False,
            metadata={
                "gateway_session_key": entry.session_key,
                "gateway_session_id": session_id,
                "gateway_session_strict": True,
                "native_request_ref": native_request_ref,
                "native_submit_authenticated": True,
                # Weave: the caller's request id (weave-api's Ledger command id)
                # names the agent turn this admission starts. It rides the event,
                # so a queued submit keeps its own id; the gateway stages it
                # one-shot right before the run (gateway/run.py).
                "external_request_id": external_request_id,
            },
        )
        # The adapter guard is set synchronously before its background task is
        # spawned.  Queue directly through the runner's existing FIFO helper,
        # never through its ambient steer/interrupt policy.
        self._native_submit_external_ids[native_request_ref] = external_request_id
        if len(self._native_submit_external_ids) > 1_024:
            self._native_submit_external_ids.pop(next(iter(self._native_submit_external_ids)), None)
        busy = entry.session_key in self._active_sessions
        is_session_running = getattr(runner, "_is_session_running", None)
        if not busy and callable(is_session_running):
            busy = bool(is_session_running(entry.session_key))
        if expected_active_ref is not None and busy_mode != "queue" and self._native_submit_active_ref(entry.session_key) != expected_active_ref:
            return "target_changed"
        if busy:
            # Weave (WEV-2108): the caller's busy_mode, never the ambient policy. steer folds the
            # text into the running turn; interrupt stops it and runs this submit next. Either
            # falls back to the FIFO when the running agent cannot take it, so nothing is lost.
            agent = self._native_submit_running_agent(runner, entry.session_key)
            active_ref = self._native_submit_active_ref(entry.session_key)
            if busy_mode == "steer" and active_ref and agent is not None and hasattr(agent, "steer"):
                try:
                    _steer = getattr(agent, "steer_if_open", None)
                    steered = bool(_steer(message) if callable(_steer) else agent.steer(message))
                except Exception:  # health: allow BLE001,S110 -- HTTP/credential boundary: return typed refusal without exception contents.
                    logger.warning("[api_server] native submit steer failed", exc_info=True)
                    steered = False
                if steered:
                    self._record_native_submit_handoff(native_request_ref, "steered")
                    # Open until the turn it joined ends: that turn's finish closes it (below).
                    self._native_submit_steered.setdefault(active_ref, []).append(native_request_ref)
                    self._native_submit_event(native_request_ref, "turn.started", steered_into=active_ref)
                    return "steered"
            # The gateway's own interrupt paths demote to queue while subagents run (#30170) or
            # compression is in flight (#56391); this path keeps both guards.
            from gateway.run import GatewayRunner

            compressing = getattr(runner, "_session_has_compression_in_flight", None)
            interruptible = (busy_mode == "interrupt" and agent is not None and active_ref
                             and hasattr(agent, "interrupt") and not GatewayRunner._agent_has_active_subagents(agent))
            compression = bool(interruptible and callable(compressing) and await compressing(entry.session_key))
            if expected_active_ref is not None and busy_mode != "queue" and self._native_submit_active_ref(entry.session_key) != expected_active_ref:
                return "target_changed"
            runner._enqueue_fifo(entry.session_key, event, self)
            self._record_native_submit_handoff(native_request_ref, "queued")
            if (
                interruptible and not compression
                # Fence after the await: the same turn must still be running on the same agent, and
                # the guards must still hold, or this request could interrupt itself or a subagent.
                and self._native_submit_active_ref(entry.session_key) == active_ref
                and self._native_submit_running_agent(runner, entry.session_key) is agent
                and not GatewayRunner._agent_has_active_subagents(agent)
            ):
                try:
                    agent.interrupt(message)
                except Exception:  # health: allow BLE001,S110 -- HTTP/credential boundary: return typed refusal without exception contents.
                    logger.warning("[api_server] native submit interrupt failed", exc_info=True)
            return "queued"
        started = asyncio.get_running_loop().create_future()
        self._native_submit_started[native_request_ref] = started
        self._record_native_submit_handoff(native_request_ref, started)
        await self.handle_message(event)
        return await self._wait_native_submit_start(native_request_ref, started)

    @staticmethod
    def _native_submit_fingerprint(message: str, busy_mode: str, internal: bool = False) -> str:
        """Weave (WEV-2108): the idempotency identity of one submit. A retry that changes busy_mode is
        changed input (409), not a replay. queue keeps the message-only hash that records written
        before busy modes existed carry, so their identical retries still replay across a deploy."""
        digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
        fingerprint = digest if busy_mode == "queue" else f"{busy_mode}:{digest}"
        # Preserve legacy user identities, but do not replay a user request as
        # an internal notification (or vice versa) when its flag changes.
        return f"internal:{fingerprint}" if internal else fingerprint

    @staticmethod
    def _native_submit_running_agent(runner: Any, session_key: str) -> Any:
        """The session's live agent, or None while it is still starting (sentinel) or absent."""
        peek = getattr(runner, "_peek_session_state", None)
        state = peek(session_key) if callable(peek) else None
        agent = getattr(getattr(state, "turn", None), "agent", None)
        from gateway.run import _AGENT_PENDING_SENTINEL

        return None if agent is None or agent is _AGENT_PENDING_SENTINEL else agent

    async def _handle_session_submit(self, request: "web.Request") -> "web.Response":
        self._native_awake_loop = asyncio.get_running_loop()
        """POST /api/sessions/{id}/submit — native gateway admission receipt."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        body, err = await self._read_json_body(request)
        if err:
            return err
        parsed, err = self._native_submit_request(body)
        if err:
            return err
        assert parsed is not None
        db = await self._ensure_session_db_async()
        if db is None:
            return web.json_response(_openai_error("Session database unavailable", code="session_db_unavailable"), status=503)

        profile = _api_request_profile.get() or "default"
        runner = self.gateway_runner
        if runner is None or not getattr(runner, "_running", False):
            return web.json_response(
                _openai_error("Native gateway unavailable", code="native_gateway_unavailable"), status=503
            )
        source = SessionSource(
            platform=Platform.API_SERVER, chat_id=session_id, chat_type="dm",
            user_id="api_server", user_name="API server",
            profile=_api_request_profile.get() or None,
        )
        return await self._native_submit_registered(session_id, body, parsed, db, profile, runner, source)

    async def _native_submit_registered(self, session_id, body, parsed, db, profile, runner, source):
        external_request_id, message, busy_mode, expected_active_ref, internal = parsed
        async with self._native_lifecycle_lock(session_id):
            from gateway.native_bot_admission import enabled as native_bot_sessions
            if not native_bot_sessions():
                credential_available = getattr(runner, "session_credential_available", None)
                if not callable(credential_available) or not await asyncio.to_thread(credential_available, source, session_id):
                    return web.json_response(_openai_error("Credential unavailable", code="credential_unavailable"), status=409)
            native_request_ref = uuid.uuid4().hex
            from gateway import native_awake
            registration = asyncio.create_task(asyncio.to_thread(
                db.register_native_session_submit,
                session_id,
                external_request_id=external_request_id,
                message_sha256=self._native_submit_fingerprint(message, busy_mode, internal),
                native_request_ref=native_request_ref,
            ))
            try:
                result = await asyncio.shield(registration)
            except asyncio.CancelledError:
                try:
                    result = await native_awake.finish_owned(registration)
                    if result["outcome"] == "inserted":
                        await native_awake.finish_owned(asyncio.create_task(asyncio.to_thread(
                            db.remove_native_session_submit, external_request_id=external_request_id,
                            native_request_ref=result["native_request_ref"],
                        )))
                finally:
                    raise
            outcome = result["outcome"]
            if outcome == "session_not_found":
                return web.json_response(_openai_error("Session not found", code="session_not_found"), status=404)
            if outcome == "idempotency_conflict":
                return web.json_response(
                    _openai_error("external_request_id was reused with different input", code="native_submit_idempotency_conflict"), status=409
                )
            native_request_ref = result["native_request_ref"]
            if outcome == "identical_retry" and result.get("admission") in {"streaming", "queued", "steered"}:
                admission = result["admission"]
            else:
                owns_hold = False
                try:
                    owns_hold = native_request_ref not in self.__dict__.setdefault("_native_awake_refs", set())
                    await self._native_acquire_hold(native_request_ref)
                    admission_options = self._native_admission_options(busy_mode, expected_active_ref, internal)
                    if native_request_ref in self._native_submit_handoffs:
                        handed_off = self._native_submit_handoffs[native_request_ref]
                        if handed_off == "dispatch_pending":
                            raise RuntimeError("native dispatch outcome unresolved")
                        admission = (await self._wait_native_submit_start(native_request_ref, handed_off, retry=True)
                                     if asyncio.isfuture(handed_off) else handed_off)
                    else:
                        admission = await self._admit_native_session_submit(
                            session_id, message, native_request_ref,
                            external_request_id=external_request_id,
                            **admission_options,
                        )
                    if admission == "target_changed":
                        self._release_native_awake(native_request_ref)
                        await asyncio.to_thread(db.remove_native_session_submit, external_request_id=external_request_id, native_request_ref=native_request_ref)
                        return web.json_response(_openai_error("Active turn changed", code="native_submit_target_changed"), status=409)
                    if admission == "answered_approval":
                        # DEC-415: the send answered the running turn's approval; no turn of its own, so the
                        # running turn's hold keeps the Sprite awake and this ref owns nothing.
                        self._release_native_awake(native_request_ref)
                        await asyncio.to_thread(db.set_native_session_submit_admission,
                                                native_request_ref=native_request_ref, admission=admission)
                        return web.json_response({"object": "hermes.session.admission", "session_id": session_id,
                                                  "external_request_id": external_request_id, "admission": admission,
                                                  "native_request_ref": native_request_ref}, status=202)
                    # A positive admission is owned by execution, not this HTTP task.
                    self._record_native_submit_handoff(native_request_ref, admission)
                    if admission == "queued":
                        self.__dict__.setdefault("_native_queued_submit_refs", set()).add(native_request_ref)
                    await asyncio.to_thread(
                        db.set_native_session_submit_admission,
                        native_request_ref=native_request_ref,
                        admission=admission,
                    )
                except asyncio.CancelledError:
                    if native_request_ref not in self._native_submit_handoffs:
                        if owns_hold:
                            self._release_native_awake(native_request_ref)
                        await native_awake.finish_owned(asyncio.create_task(asyncio.to_thread(
                            db.remove_native_session_submit, external_request_id=external_request_id,
                            native_request_ref=native_request_ref,
                        )))
                    raise
                except Exception:  # health: allow BLE001,S110 -- HTTP/credential boundary: return typed refusal without exception contents.
                    if native_request_ref not in self._native_submit_handoffs:
                        self._release_native_awake(native_request_ref)
                        await asyncio.to_thread(
                            db.remove_native_session_submit,
                            external_request_id=external_request_id,
                            native_request_ref=native_request_ref,
                        )
                    return web.json_response(
                        _openai_error("Native gateway admission unavailable", code="native_admission_unavailable"), status=503
                    )

            if "hub_tools" in body:
                from gateway.hub_revision import HubRevision
                revisions = self.__dict__.setdefault("_hub_revisions", {})
                revisions.setdefault(profile, HubRevision(self)).request(body["hub_tools"]["revision"])
            return web.json_response({
                "object": "hermes.session.admission",
                "session_id": session_id,
                "external_request_id": external_request_id,
                "admission": admission,
                "native_request_ref": native_request_ref,
                **({"hub_tools": body["hub_tools"]} if "hub_tools" in body else {}),
            }, status=202)

    @staticmethod
    def _native_admission_options(busy_mode, expected_active_ref, internal):
        options = {}
        if busy_mode != "queue":
            options["busy_mode"] = busy_mode
        if expected_active_ref is not None:
            options["expected_active_ref"] = expected_active_ref
        if internal:
            options["internal"] = True
        return options

    async def _native_acquire_hold(self, native_request_ref):
        from gateway import native_awake
        owns_hold = False
        if native_request_ref not in self.__dict__.setdefault("_native_awake_refs", set()):
            owns_hold = True
            acquisition = asyncio.create_task(native_awake.acquire(native_request_ref))
            try:
                await asyncio.shield(acquisition)
            except asyncio.CancelledError as cancelled:
                try:
                    await native_awake.finish_owned(acquisition)
                except Exception:  # health: allow BLE001,S110 -- HTTP/credential boundary: return typed refusal without exception contents.
                    pass  # Drain failed acquisition, but still release our lease.
                finally:
                    # Even a lost helper ack may have created our lease.
                    self._native_awake_refs.add(native_request_ref)
                raise cancelled
            self._native_awake_refs.add(native_request_ref)
        return owns_hold

    @staticmethod
    def _native_submit_end_subscriber(
        queue: asyncio.Queue, loop: asyncio.AbstractEventLoop,
    ) -> None:
        def _close() -> None:
            with suppress(asyncio.QueueEmpty):
                while True:
                    queue.get_nowait()
            queue.put_nowait(None)
        try:
            on_subscriber_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_subscriber_loop = False
        _close() if on_subscriber_loop else loop.call_soon_threadsafe(_close)

    def _reconcile_native_submit_evidence(self, native_request_ref: str, admission: str = "streaming") -> None:
        """An observed start/terminal proves admission, even if its RPC ack was lost."""
        if self._native_submit_handoffs.get(native_request_ref) != "dispatch_pending":
            return
        db = self._ensure_session_db()
        if db is not None:
            # Persist before forgetting the ambiguity; replay survives adapter restart.
            db.set_native_session_submit_admission(native_request_ref=native_request_ref, admission=admission)
            self._record_native_submit_handoff(native_request_ref, admission)

    def _native_submit_event(
        self, native_request_ref: str, event_type: str, *, _broadcast: bool = True,
        **fields: Any,
    ) -> dict[str, Any]:
        """Publish one bounded live projection without retaining an event log."""
        if event_type in {"turn.started", "turn.completed", "turn.failed"}:
            self._reconcile_native_submit_evidence(native_request_ref)
        with self._native_submit_event_lock:
            sequence = self._native_submit_sequences.get(native_request_ref, 0) + 1
            self._native_submit_sequences[native_request_ref] = sequence
        event = {
            "native_request_ref": native_request_ref,
            "sequence": sequence,
            "type": event_type,
            **fields,
        }
        if _broadcast:
            self._native_submit_chat_sync(native_request_ref, event)
        subscriber = self._native_submit_subscribers.get(native_request_ref)
        if _broadcast and subscriber is not None:
            queue, loop = subscriber

            def _enqueue(queue=queue, event=event) -> None:
                current = self._native_submit_subscribers.get(native_request_ref)
                if current is None or current[0] is not queue:
                    return
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    self._native_submit_subscribers.pop(native_request_ref, None)
                    self._native_submit_end_subscriber(queue, loop)
            try:
                on_subscriber_loop = asyncio.get_running_loop() is loop
            except RuntimeError:
                on_subscriber_loop = False
            if on_subscriber_loop:
                _enqueue()
            else:
                loop.call_soon_threadsafe(_enqueue)
        return event

    def _release_native_awake(self, native_request_ref: str) -> None:
        loop = getattr(self, "_native_awake_loop", None)
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if loop is not None and current is not loop:
            if not loop.is_closed():
                loop.call_soon_threadsafe(self._release_native_awake, native_request_ref)
            return
        refs = self.__dict__.setdefault("_native_awake_refs", set())
        if native_request_ref in refs:
            refs.discard(native_request_ref)
            from gateway import native_awake
            native_awake.release(native_request_ref)

    def _native_submit_close(self, native_request_ref: str, event_type: str, **fields: Any) -> None:
        terminal = self._native_submit_event(native_request_ref, event_type, **fields)
        self._native_submit_terminals[native_request_ref] = terminal
        if len(self._native_submit_terminals) > 1_024:
            evicted = next(iter(self._native_submit_terminals))
            self._native_submit_terminals.pop(evicted, None)
            self._native_submit_ref_sessions.pop(evicted, None)
        self._native_submit_events.pop(native_request_ref, None)
        self._native_submit_sequences.pop(native_request_ref, None)
        subscriber = self._native_submit_subscribers.pop(native_request_ref, None)
        if subscriber is not None:
            queue, _loop = subscriber
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                queue.put_nowait(None)

    def _native_submit_active_ref(self, session_key: str) -> Optional[str]:
        native_request_ref = self._native_submit_active_refs.get(session_key)
        return None if native_request_ref in self._native_submit_terminals else native_request_ref

    def _native_submit_delta(self, session_key: str, delta: Any) -> None:
        native_request_ref = self._native_submit_active_ref(session_key)
        if native_request_ref and isinstance(delta, str) and delta:
            self._native_submit_event(
                native_request_ref, "assistant.delta", delta=delta[:4_096],
            )

    def _native_submit_tool_started(
        self, session_key: str, tool_call_id: Any, tool_name: Any,
    ) -> None:
        native_request_ref = self._native_submit_active_ref(session_key)
        if native_request_ref:
            self._native_submit_event(
                native_request_ref,
                "tool.started",
                tool_call_id=str(tool_call_id)[:256],
                tool_name=str(tool_name)[:256],
            )

    def _native_submit_final(self, session_key: str, content: Any) -> None:
        native_request_ref = self._native_submit_active_ref(session_key)
        if native_request_ref and isinstance(content, str) and content:
            # Weave (WEV-1726): the final names the admission it answers, so a consumer
            # attributes by id rather than by arrival order.
            external_request_id = self._native_submit_external_ids.get(native_request_ref)
            self._native_submit_event(
                native_request_ref, "assistant.final", content=content[:16_384],
                # Omit rather than send null: a consumer without the id falls back to its
                # order rule; an explicit null would be a third, undefined state.
                **({"external_request_id": external_request_id} if isinstance(external_request_id, str) and external_request_id else {}),
            )

    async def _handle_native_submit_events(self, request: "web.Request") -> "web.StreamResponse":
        """Observe one admitted native turn; this endpoint never starts work."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        native_request_ref = request.match_info["native_request_ref"]
        if self._native_submit_ref_sessions.get(native_request_ref) != (
            _api_request_profile.get() or "default", session_id
        ):
            return web.json_response(_openai_error("Native admission was not found", code="native_admission_not_found"), status=404)
        if native_request_ref in self._native_submit_terminals:
            # Keep one bounded terminal receipt, not a second event log. Reconnect
            # must distinguish a completed/failed turn from a broken connection.
            terminal = self._native_submit_terminals[native_request_ref]
            return web.Response(body=_sse_frame(terminal, event=terminal["type"]),
                                content_type="text/event-stream", headers={"Cache-Control": "no-store"})

        queue: asyncio.Queue = asyncio.Queue(maxsize=32)
        old_subscriber = self._native_submit_subscribers.pop(native_request_ref, None)
        if old_subscriber is not None:
            self._native_submit_end_subscriber(*old_subscriber)
        self._native_submit_subscribers[native_request_ref] = (
            queue, asyncio.get_running_loop(),
        )
        # A reconnect gets only the one currently pending clarification; all
        # other events are live-only projections of the existing native turn.
        for clarify in self._native_submit_events.get(native_request_ref, []):
            queue.put_nowait(self._native_submit_event(native_request_ref, "clarify.request", _broadcast=False, **{
                key: value for key, value in clarify.items() if key not in {"type", "native_request_ref"}
            }))
        # Weave: likewise the approvals still pending in the approval queue.
        for approval in self._native_submit_pending_approvals(native_request_ref)[:16]:
            queue.put_nowait(self._native_submit_event(
                native_request_ref, "approval.request", _broadcast=False, **approval,
            ))

        response = web.StreamResponse(status=200, headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })
        await response.prepare(request)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS,
                    )
                except TimeoutError:
                    await response.write(b": keepalive\n\n")
                    continue
                if event is None:
                    break
                await response.write(_sse_frame(event, event=event["type"]))
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            pass
        finally:
            current = self._native_submit_subscribers.get(native_request_ref)
            if current is not None and current[0] is queue:
                self._native_submit_subscribers.pop(native_request_ref, None)
        return response

    async def _handle_native_submit_clarify_events(self, request: "web.Request") -> "web.Response":
        """Return only pending native clarification requests for one admission."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        native_request_ref = request.match_info["native_request_ref"]
        if self._native_submit_ref_sessions.get(native_request_ref) != (
            _api_request_profile.get() or "default", session_id
        ):
            return web.json_response(_openai_error("Native admission was not found", code="native_admission_not_found"), status=404)
        events = self._native_submit_events.get(native_request_ref, [])
        return web.json_response({"object": "list", "data": events})

    async def _handle_native_submit_clarify_response(self, request: "web.Request") -> "web.Response":
        """Resolve exactly the native waiter named by admission ref and clarify id."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        native_request_ref = request.match_info["native_request_ref"]
        clarify_id = request.match_info["clarify_id"]
        if self._native_submit_ref_sessions.get(native_request_ref) != (
            _api_request_profile.get() or "default", session_id
        ):
            return web.json_response(_openai_error("Native admission was not found", code="native_admission_not_found"), status=404)
        state = self._native_submit_clarifies.get((native_request_ref, clarify_id))
        if state is None:
            return web.json_response(_openai_error("Native clarification was not found", code="native_clarify_not_found"), status=404)
        if state != "pending":
            return web.json_response(_openai_error("Native clarification is terminal", code="native_clarify_terminal"), status=409)
        body, err = await self._read_json_body(request)
        if err:
            return err
        if set(body) != {"response"} or not isinstance(body.get("response"), str) or not body["response"].strip():
            return web.json_response(_openai_error("Invalid clarification response", code="invalid_native_clarify_response"), status=400)
        try:
            if len(body["response"].encode("utf-8")) > 65_536:
                raise ValueError
        except (UnicodeEncodeError, ValueError):
            return web.json_response(_openai_error("Invalid clarification response", code="invalid_native_clarify_response"), status=400)
        from tools.clarify_gateway import resolve_gateway_clarify

        from gateway.native_bot_admission import respond
        resolved = await respond(self, native_request_ref, clarify_id, body['response'], "clarify")
        if resolved is None:
            resolved = resolve_gateway_clarify(clarify_id, body['response'])
        if not resolved:
            self._native_submit_clarifies[(native_request_ref, clarify_id)] = "terminal"
            self._native_submit_events.pop(native_request_ref, None)
            return web.json_response(_openai_error("Native clarification is terminal", code="native_clarify_terminal"), status=409)
        self._native_submit_clarifies[(native_request_ref, clarify_id)] = "resolved"
        self._native_submit_events.pop(native_request_ref, None)
        return web.json_response({"object": "hermes.session.clarify.response", "resolved": True})

    async def send_clarify(
        self, chat_id: str, question: str, choices: Optional[list], clarify_id: str,
        session_key: str, metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Expose the existing clarify waiter without inventing a prompt system."""
        native_request_ref = self._native_submit_active_ref(session_key)
        if not native_request_ref:
            from gateway.platforms.api_server_authority_runs import send_clarify
            return await send_clarify(self, chat_id=chat_id, clarify_id=clarify_id)
        from tools.clarify_gateway import get_clarify_metadata

        clarify = get_clarify_metadata(clarify_id) or {}
        safe_choices = [str(choice)[:512] for choice in clarify.get("choices", choices or [])[:32]]
        self._native_submit_events.setdefault(native_request_ref, []).append({
            "type": "clarify.request",
            "native_request_ref": native_request_ref,
            "clarify_id": clarify_id,
            "question": str(clarify.get("question", question))[:4096],
            "choices": safe_choices,
            "multi_select": bool(clarify.get("multi_select", False)),
        })
        self._native_submit_event(
            native_request_ref,
            "clarify.request",
            clarify_id=clarify_id,
            question=str(clarify.get("question", question))[:4096],
            choices=safe_choices,
            multi_select=bool(clarify.get("multi_select", False)),
        )
        self._native_submit_clarifies[(native_request_ref, clarify_id)] = "pending"
        return SendResult(success=True, message_id=clarify_id)

    @staticmethod
    def _native_approval_projection(approval: dict[str, Any]) -> dict[str, Any]:
        # The queue payload honours security.redact_secrets; this egress is a
        # safety boundary (same as gateway/run.py _redact_approval_command), so
        # human-visible text is force-redacted for the live event AND replay.
        def visible(key: str) -> str:
            return redact_sensitive_text(str(approval.get(key) or ""), force=True)[:4096]

        return {
            "request_id": str(approval.get("request_id") or "")[:256],
            "pattern_key": str(approval.get("pattern_key") or "")[:512],
            "description": visible("description"),
            "command": visible("command"),
            "choices": list(NativeSessionAPI._NATIVE_APPROVAL_CHOICES),
        }

    def _native_submit_pending_approvals(self, native_request_ref: str) -> list[dict[str, Any]]:
        session_key = self._native_submit_approval_keys.get(native_request_ref)
        # Approvals are queued per session key; only the admission that owns the
        # session's live turn may see or resolve them. An outer admission still
        # registered while its queued follow-up runs owns nothing.
        if not session_key or self._native_submit_active_ref(session_key) != native_request_ref:
            return []
        from gateway.native_bot_admission import pending
        prompts = pending(self, native_request_ref, "approval")
        if prompts is not None:
            return [self._native_approval_projection({**p, "request_id": p["prompt_id"]}) for p in prompts]
        from tools.approval import list_gateway_approvals

        return [
            self._native_approval_projection(approval)
            for approval in list_gateway_approvals(session_key)
            if approval.get("request_id")
        ]

    async def send_exec_approval(
        self, chat_id: str, command: str, session_key: str, description: str = "",
        metadata: Optional[dict[str, Any]] = None, allow_permanent: bool = True,
        allow_session: bool = True, smart_denied: bool = False,
    ) -> SendResult:
        """Publish pending gateway approvals as ``approval.request`` on the native stream."""
        native_request_ref = self._native_submit_active_ref(session_key)
        if not native_request_ref or self._native_submit_approval_keys.get(native_request_ref) != session_key:
            return SendResult(success=False, error="native approval surface unavailable")
        sent = self._native_submit_approvals_sent.setdefault(native_request_ref, set())
        published = []
        for approval in self._native_submit_pending_approvals(native_request_ref):
            if approval["request_id"] in sent:
                continue
            sent.add(approval["request_id"])
            self._native_submit_event(native_request_ref, "approval.request", **approval)
            published.append(approval["request_id"])
        if not published:
            return SendResult(success=False, error="no pending approval to publish")
        return SendResult(success=True, message_id=published[-1])

    async def _handle_native_submit_approval_events(self, request: "web.Request") -> "web.Response":
        """Return the pending approvals of one attended native admission (reconnect)."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        native_request_ref = request.match_info["native_request_ref"]
        if self._native_submit_ref_sessions.get(native_request_ref) != (
            _api_request_profile.get() or "default", session_id
        ):
            return web.json_response(_openai_error("Native admission was not found", code="native_admission_not_found"), status=404)
        events = [
            {"type": "approval.request", "native_request_ref": native_request_ref, **approval}
            for approval in self._native_submit_pending_approvals(native_request_ref)
        ]
        return web.json_response({"object": "list", "data": events})

    async def _handle_native_submit_approval_response(self, request: "web.Request") -> "web.Response":
        """Resolve exactly the pending approval named by admission ref and request id."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        native_request_ref = request.match_info["native_request_ref"]
        request_id = request.match_info["request_id"]
        if self._native_submit_ref_sessions.get(native_request_ref) != (
            _api_request_profile.get() or "default", session_id
        ):
            return web.json_response(_openai_error("Native admission was not found", code="native_admission_not_found"), status=404)
        body, err = await self._read_json_body(request)
        if err:
            return err
        choice = body.get("choice")
        if set(body) != {"choice"} or choice not in self._NATIVE_APPROVAL_CHOICES:
            return web.json_response(_openai_error(
                "Invalid approval choice; expected one of: once, deny",
                code="invalid_native_approval_choice",
            ), status=400)
        from gateway.native_bot_admission import respond
        result = await respond(self, native_request_ref, request_id, choice, "approval")
        if result is not None:
            if not result:
                return web.json_response(_openai_error("Native approval is not pending", code="native_approval_not_pending"), status=409)
            return web.json_response({"object": "hermes.session.approval.response", "resolved": True, "choice": choice})
        session_key = self._native_submit_approval_keys.get(native_request_ref)
        if not session_key or not any(
            approval["request_id"] == request_id
            for approval in self._native_submit_pending_approvals(native_request_ref)
        ):
            return web.json_response(_openai_error("Native approval is not pending", code="native_approval_not_pending"), status=409)
        from tools.approval import resolve_gateway_approval

        if not resolve_gateway_approval(session_key, choice, request_id=request_id):
            return web.json_response(_openai_error("Native approval is not pending", code="native_approval_not_pending"), status=409)
        return web.json_response({"object": "hermes.session.approval.response", "resolved": True, "choice": choice})

    async def _on_native_submit_started(self, event: MessageEvent, session_key: str) -> None:
        native_request_ref = (getattr(event, "metadata", None) or {}).get("native_request_ref")
        if isinstance(native_request_ref, str) and native_request_ref:
            self._native_submit_active_refs[session_key] = native_request_ref
            from tools.approval import mark_api_session_attended, native_submit_attended_enabled

            if session_key and native_submit_attended_enabled():
                self._native_submit_approval_keys[native_request_ref] = session_key
                mark_api_session_attended(session_key)
            source = getattr(event, "source", None)
            chat_id = getattr(source, "chat_id", None)
            if isinstance(chat_id, str) and chat_id:
                self._native_submit_ref_sessions.setdefault(
                    native_request_ref,
                    (getattr(source, "profile", None) or "default", chat_id),
                )
            started = self._native_submit_started.get(native_request_ref)
            try:
                db = await self._ensure_session_db_async()
                if db is None:
                    raise RuntimeError("session database unavailable")
                await asyncio.to_thread(
                    db.set_native_session_submit_admission,
                    native_request_ref=native_request_ref,
                    admission="streaming",
                )
            except Exception as exc:  # health: allow BLE001,S110 -- HTTP/credential boundary: return typed refusal without exception contents.
                if started is not None and not started.done():
                    started.set_exception(exc)
                    return
                raise
            else:
                if started is not None and not started.done():
                    started.set_result("streaming")
                queued_refs = self.__dict__.setdefault("_native_queued_submit_refs", set())
                if native_request_ref in queued_refs:
                    queued_refs.discard(native_request_ref)
                    self._native_submit_event(native_request_ref, "turn.started")
                # Weave (WEV-1726): submits folded into this turn while it queued get an honest
                # terminal now, attributed to the turn that absorbed them, so their consumers
                # settle instead of hanging until stale reconciliation.
                self._native_submit_close_merged(event, native_request_ref, failed=False)

    def _native_submit_close_merged(self, event: Any, survivor_ref: str, *, failed: bool) -> None:
        """Weave (WEV-1726): settle every ref folded into ``event`` exactly once.

        Called from the survivor's successful start (merged into a running turn)
        AND from its failure/cancellation finish (third review: a survivor whose
        start aborted closed itself but stranded its folded siblings). Idempotent
        via ``settled_native_request_refs`` on the survivor's own metadata (the
        bounded terminal cache is only a secondary guard); a retry of either
        path emits nothing new.
        """
        meta = getattr(event, "metadata", None)
        merged = (meta or {}).get("merged_native_request_refs") if isinstance(meta, dict) else None
        if not isinstance(merged, list):
            return
        # Idempotency lives on the EVENT, not in the bounded terminal cache (fourth review:
        # that cache evicts after 1,024 closes, so a long survivor turn let finish re-emit a
        # sibling's start and terminal). Once settled, the survivor's own metadata says so.
        settled = meta.setdefault("settled_native_request_refs", [])
        if not isinstance(settled, list):
            return
        queued_refs = self.__dict__.setdefault("_native_queued_submit_refs", set())
        for sibling in merged:
            if not isinstance(sibling, str) or not sibling or sibling in settled or sibling in self._native_submit_terminals:
                continue
            settled.append(sibling)
            queued_refs.discard(sibling)
            self._native_submit_event(sibling, "turn.started", merged_into=survivor_ref)
            self._native_submit_close(sibling, "turn.failed" if failed else "turn.completed", merged_into=survivor_ref)

    async def _on_native_submit_finished(self, event: MessageEvent, session_key: str) -> None:
        native_request_ref = (getattr(event, "metadata", None) or {}).get("native_request_ref")
        if not isinstance(native_request_ref, str) or not native_request_ref:
            return
        if self._native_submit_active_refs.get(session_key) == native_request_ref:
            self._native_submit_active_refs.pop(session_key, None)
        approval_key = self._native_submit_approval_keys.pop(native_request_ref, None)
        self._native_submit_approvals_sent.pop(native_request_ref, None)
        if approval_key and approval_key not in self._native_submit_approval_keys.values():
            from tools.approval import mark_api_session_attended

            mark_api_session_attended(approval_key, False)
        for key, state in list(self._native_submit_clarifies.items()):
            if key[0] == native_request_ref and state == "pending":
                self._native_submit_clarifies[key] = "terminal"
        failed = bool((getattr(event, "metadata", None) or {}).get("native_submit_failed"))
        # A survivor that never reached its successful start still owns its folded siblings:
        # settle them with the survivor's outcome. Already-closed siblings are skipped.
        self._native_submit_close_merged(event, native_request_ref, failed=failed)
        for steered in self._native_submit_steered.pop(native_request_ref, ()):
            if steered not in self._native_submit_terminals:
                self._native_submit_close(
                    steered, "turn.failed" if failed else "turn.completed", steered_into=native_request_ref,
                )
        self._native_submit_close(
            native_request_ref, "turn.failed" if failed else "turn.completed",
        )

    async def _handle_fork_session(self, request: "web.Request") -> "web.Response":
        """POST /api/sessions/{session_id}/fork — atomic native safe boundary."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        source_id = request.match_info["session_id"]
        body, err = await self._read_json_body(request)
        if err:
            return err
        if not ({'external_rotation_id', 'boundary_turn_id'} & set(body)):
            return await self._upstream_fork_session(request)
        if set(body) != {"external_rotation_id", "boundary_turn_id"}:
            return web.json_response(
                _openai_error("Invalid session fork schema", code="invalid_session_fork_schema"), status=400,
            )
        rotation_id = body["external_rotation_id"]
        boundary_turn_id = body["boundary_turn_id"]
        try:
            valid_ids = all(
                isinstance(value, str) and str(uuid.UUID(value)) == value for value in (rotation_id, boundary_turn_id)
            )
        except (ValueError, AttributeError):
            valid_ids = False
        if not valid_ids:
            return web.json_response(
                _openai_error("Invalid session fork schema", code="invalid_session_fork_schema"), status=400,
            )
        _, err = await self._get_existing_session_or_404(source_id)
        if err:
            return err
        db = await self._ensure_session_db_async()
        runner = self.gateway_runner
        if runner is None or not getattr(runner, "_running", False):
            return web.json_response(
                _openai_error("Native gateway unavailable", code="native_gateway_unavailable"), status=503,
            )
        return await self._native_fork_locked(source_id, rotation_id, boundary_turn_id, db, runner)

    async def _native_fork_locked(self, source_id, rotation_id, boundary_turn_id, db, runner):
        successor_id = f"weave-{rotation_id}"
        if successor_id == source_id:
            return web.json_response(
                _openai_error("Session fork conflicts with native lineage", code="session_fork_conflict"), status=409,
            )
        source_kwargs = dict(platform=Platform.API_SERVER, chat_type="dm", user_id="api_server",
                             user_name="API server", profile=_api_request_profile.get() or None)
        source = SessionSource(chat_id=source_id, **source_kwargs)
        successor = SessionSource(chat_id=successor_id, **source_kwargs)
        store = runner.session_store
        session_key = store._generate_session_key(source)
        successor_key = store._generate_session_key(successor)
        await runner.async_session_store._ensure_loaded()

        def unavailable():
            return web.json_response(
                _openai_error("Session boundary unavailable", code="session_boundary_unavailable"), status=409,
                headers={"Retry-After": "1"},
            )

        first_lock, second_lock = sorted((source_id, successor_id))
        async with self._native_lifecycle_lock(first_lock), self._native_lifecycle_lock(second_lock):
            if await asyncio.to_thread(db.get_session, successor_id) is None:
                from gateway.session import SessionResetRequired

                try:
                    bound = await runner.async_session_store.bind_existing_session(
                        source, source_id, reopen=False, raise_on_reset=True,
                    )
                except SessionResetRequired as exc:
                    return self._session_reset_required(exc.cause)
                if bound is None or bound.session_id != source_id:
                    return unavailable()
            queued = session_key in self._pending_messages or bool(
                getattr(runner._peek_session_state(session_key), "conversation", None)
                and runner._queue_depth(session_key, adapter=self)
            )
            entry = runner.session_store._entries.get(session_key)
            active = (
                session_key in self._active_sessions
                or runner._is_session_running(session_key)
                or bool(entry and entry.active_turn_token)
            )
            if queued or active:
                return unavailable()
            try:
                outcome = await asyncio.to_thread(
                    db.safe_fork_session, source_id, successor_id,
                    before_commit=lambda: runner.revoke_session_credential(
                        source, source_id
                    ),
                )
            except Exception:  # health: allow BLE001,S110 -- HTTP/credential boundary: return typed refusal without exception contents.
                return unavailable()
            if outcome == "boundary_unavailable":
                return unavailable()
            if outcome == "conflict":
                return web.json_response(
                    _openai_error("Session fork conflicts with native lineage", code="session_fork_conflict"), status=409,
                )
            try:
                switched = store.lookup_by_session_key(successor_key)
                if switched is None:
                    predecessor = store.lookup_by_session_key(session_key)
                    if predecessor is None and outcome == "identical_retry":
                        switched = await runner.async_session_store.bind_existing_session(
                            successor, successor_id, reopen=False,
                        )
                    else:
                        switched = await runner.async_session_store.switch_session(session_key, successor_id)
                if switched is None or switched.session_id != successor_id:
                    return unavailable()
                with store._lock:
                    current = store._entries.get(successor_key)
                    if current is None:
                        current = store._entries.pop(session_key, None)
                        if current is switched:
                            switched.session_key = successor_key
                            store._entries[successor_key] = switched
                    if current is not switched:
                        return unavailable()
                    store._entries.pop(session_key, None)
                rebound = await runner.async_session_store.bind_existing_session(successor, successor_id, reopen=False)
                if rebound is not switched:
                    return unavailable()
                runner._evict_cached_agent(session_key)
            except Exception:  # health: allow BLE001,S110 -- HTTP/credential boundary: return typed refusal without exception contents.
                return unavailable()

        return web.json_response({
            "outcome": outcome,
            "external_rotation_id": rotation_id,
            "boundary_turn_id": boundary_turn_id,
            "predecessor_session_id": source_id,
            "successor_session_id": successor_id,
            "native_input_queue_empty": True,
            "no_active_turn": True,
            "no_compression_or_session_mutation_lock": True,
        }, status=201 if outcome == "forked" else 200)

    async def _native_chat_lifecycle_error(self, request):
        db = await self._ensure_session_db_async()
        row = await asyncio.to_thread(db.get_session, request.match_info['session_id']) if db is not None else None
        if row and row.get('ended_at') is not None:
            return web.json_response(_openai_error('Session is ended', code='session_ended'), status=409)
        return None

    @asynccontextmanager
    async def _native_lifecycle_lock(self, session_id: str):
        key = (_api_request_profile.get() or "default", session_id)
        self._native_submit_lock_refs[key] = self._native_submit_lock_refs.get(key, 0) + 1
        lock = self._native_submit_locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                yield
        finally:
            remaining = self._native_submit_lock_refs[key] - 1
            if remaining:
                self._native_submit_lock_refs[key] = remaining
            else:
                self._native_submit_lock_refs.pop(key, None)
                self._native_submit_locks.pop(key, None)


    def _native_submit_chat_sync(self, native_request_ref, event):
        terminal = event.get("type") in {"turn.completed", "turn.failed"}
        done = lambda: self._release_native_awake(native_request_ref)
        owner = self._native_submit_ref_sessions.get(native_request_ref)
        db = self._ensure_session_db() if owner and weave_chat_sync is not None else None
        if db is None:
            if terminal:
                done()
            return
        _profile, session_id = owner

        def read_display():
            resolved = db.resolve_resume_session_id(session_id) or session_id
            return [_project_client_message(m) for m in db.get_messages(resolved, include_compacted=True, include_ancestors=True)]

        def secret(name):
            try:
                return _scoped_get_secret(name, "") or ""
            except _UnscopedSecretError:
                return ""

        if terminal:
            if not weave_chat_sync.publish(session_id, native_request_ref, event, read_display, secret, on_finished=done):
                done()
        else:
            weave_chat_sync.publish(session_id, native_request_ref, event, read_display, secret)

    def _native_submit_tool_completed(self, session_key, tool_call_id, tool_name, result):
        from agent.display import _detect_tool_failure
        ref = self._native_submit_active_ref(session_key)
        if ref:
            failed, _ = _detect_tool_failure(str(tool_name or "tool"), result)
            from gateway.native_session_cards import hub_card
            card = None if failed else hub_card(tool_name, result)
            self._native_submit_event(ref, "tool.failed" if failed else "tool.completed",
                                      tool_call_id=str(tool_call_id)[:256], tool_name=str(tool_name)[:256], **({"card": card} if card else {}))


def initialize(adapter):
    if getattr(adapter, "_harso_native_initialized", False):
        return
    adapter._harso_native_initialized = True
    if not hasattr(type(adapter), '_upstream_fork_session'):
        type(adapter)._upstream_fork_session = type(adapter)._handle_fork_session
    for name, value in NativeSessionAPI.__dict__.items():
        if not name.startswith("__"):
            setattr(type(adapter), name, value)
    for name in ("events", "sequences", "subscribers", "active_refs", "ref_sessions", "terminals",
                 "clarifies", "started", "handoffs", "external_ids", "steered", "approval_keys", "approvals_sent"):
        setattr(adapter, "_native_submit_" + name, {})
    adapter._native_submit_event_lock = threading.Lock()
    adapter._native_submit_start_timeout_s = float(adapter.config.extra.get("native_submit_start_timeout_s", 5.0))
    adapter._native_submit_locks = {}
    adapter._native_submit_lock_refs = {}
    from gateway.native_session_credentials import install
    install(adapter.gateway_runner)


def _http_routes(adapter):
    initialize(adapter)
    base = "/api/sessions/{session_id}"
    submit = base + "/submit/{native_request_ref}"
    return [(method, path, getattr(adapter, handler)) for method, path, handler in [
        ("POST", base + "/submit", "_handle_session_submit"),
        ("GET", submit + "/events", "_handle_native_submit_events"),
        ("GET", submit + "/approvals", "_handle_native_submit_approval_events"),
        ("POST", submit + "/approval/{request_id}", "_handle_native_submit_approval_response"),
        ("GET", submit + "/clarify", "_handle_native_submit_clarify_events"),
        ("POST", submit + "/clarify/{clarify_id}", "_handle_native_submit_clarify_response"),
        ("POST", base + "/append", "_handle_session_append"),
        ("POST", base + "/append/group", "_handle_session_append_group"),
        ("POST", base + "/credential/bind", "_handle_session_credential_bind"),
        ("GET", base + "/events", "_handle_session_events"),
        ("POST", base + "/fork", "_handle_fork_session"),
    ]]
