"""Harso's direct, scope-bound Hermes memory provider."""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List

from agent.message_content import flatten_message_text
from agent.memory_provider import MemoryProvider
from agent.secret_scope import get_secret

logger = logging.getLogger(__name__)
_TIMEOUT_SECONDS = 5
_MAX_CONTEXT_ITEMS = 5
_MAX_CONTEXT_TEXT = 1200
_P = r"(?:0\.[0-9]{2}|1\.00)"
_ACTION = rf"external action: (?:likely|unlikely|unsure) \({_P}\)"
# WEV-1850: the fixed vocabulary weave-api's RoutingHint.line() generates. No
# free text can match, so nothing instruction-shaped reaches the chat model.
_ROUTING_HINT = re.compile(
    rf"Routing hint: (?:(?:inline|work) \({_P}\)(?:; {_ACTION})?|none; {_ACTION})")


class HarsoWriteError(RuntimeError):
    """Content-free failure that lets D4 record an unacknowledged mirror."""


class HarsoMemoryProvider(MemoryProvider):
    """Use the private Weave API as the Harso admission boundary."""

    def __init__(self) -> None:
        self._session_id = ""

    # Resolved at call time, never snapshotted in __init__: the provider is
    # constructed once at gateway start, but on the shared host Hermes runs in
    # multiplex mode and each profile's .env is loaded per turn into an isolated
    # secret scope (gateway/run.py) that never reaches os.environ. get_secret
    # honours that scope and fails closed under multiplex, so one profile can
    # never see another's endpoint or identity.
    @property
    def _endpoint(self) -> str:
        return (get_secret("WEAVE_HARSO_ENDPOINT", "") or "").rstrip("/")

    @property
    def _profile_id(self) -> str:
        return get_secret("WEAVE_HARSO_PROFILE_ID", "") or ""

    @property
    def _profile_revision_id(self) -> str:
        return get_secret("WEAVE_HARSO_PROFILE_REVISION_ID", "") or ""

    @property
    def _bearer(self) -> str:
        return get_secret("WEAVE_API_MCP_BEARER", "") or ""

    @property
    def _route_key(self) -> str:
        return get_secret("API_SERVER_KEY", "") or ""

    @property
    def name(self) -> str:
        return "harso"

    def is_available(self) -> bool:
        return all((
            self._endpoint,
            self._profile_id,
            self._profile_revision_id,
            self._bearer,
            self._route_key,
        ))

    def unavailable_reason(self) -> str:
        return "Harso requires its endpoint, profile scope, and workload credentials."

    def initialize(self, session_id: str, **_kwargs: Any) -> None:
        self._session_id = session_id

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return []

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any] | None:
        request = urllib.request.Request(
            f"{self._endpoint}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._bearer}",
                "X-Weave-Profile-Route-Key": self._route_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
                return payload if isinstance(payload, dict) else None
        except (OSError, ValueError, urllib.error.HTTPError) as exc:
            logger.warning("Harso request unavailable: %s", exc)
            return None

    def _scope(self, session_id: str) -> Dict[str, str]:
        return {
            "profile_id": self._profile_id,
            "profile_revision_id": self._profile_revision_id,
            "hermes_session_ref": session_id,
        }

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        # The turn-context caller omits session_id; initialization binds this
        # provider to the agent's session. Explicit per-call scope still wins.
        session_id = session_id or self._session_id
        if not session_id:
            return ""
        try:
            query = flatten_message_text(query).strip()
        except Exception:
            # Malformed content must never raise into the user's turn.
            return ""
        if not query:
            return ""
        # HarsoContextInput's wire-contract ceiling, not a recall tuning knob.
        # Keep the head: user intent normally precedes pasted supporting text.
        query = query[:4096]
        response = self._post(
            "/internal/harso/context",
            {
                **self._scope(session_id),
                "query": query,
            },
        )
        if not response:
            return ""
        # Jev's advisory hint is independent of recall: it renders with or
        # without admitted memory, after items and gaps. Anything else drops.
        hint = response.get("routing_hint")
        hint = hint.strip() if isinstance(hint, str) and len(hint.strip()) <= 200 else ""
        hint = hint if _ROUTING_HINT.fullmatch(hint) else ""
        # Explicit recall status supersedes the legacy degraded boolean.
        if "recall_status" in response:
            if response["recall_status"] not in ("ok", "degraded"):
                return hint
        elif response.get("degraded") is True:
            return hint
        items = response.get("items")
        if not isinstance(items, list):
            return hint
        context = []
        for item in items[:_MAX_CONTEXT_ITEMS]:
            if not isinstance(item, dict):
                continue
            citation, text = item.get("citation"), item.get("text")
            citations = item.get("citations")
            # Wire-contract ceiling per entry, not a recall-breadth tuning knob.
            if (isinstance(citations, list) and citations
                    and all(isinstance(ref, str) and ref.strip() for ref in citations)):
                citation = " ".join(citations[:64])
            if (isinstance(citation, str) and citation.strip()
                    and isinstance(text, str) and text[:_MAX_CONTEXT_TEXT].strip()):
                context.append(f"{citation} {text[:_MAX_CONTEXT_TEXT]}")
        # Gaps may annotate admitted evidence, never create context by themselves.
        gaps = response.get("gaps")
        if context and isinstance(gaps, list):
            reasons = [gap["reason"] for gap in gaps
                       if isinstance(gap, dict) and gap.get("reason") in (
                           "missing", "stale", "contradictory", "privacy-excluded", "budget-excluded"
                       )][:5]
            if reasons:
                context.append("Memory gaps: " + ", ".join(reasons))
        if hint:
            context.append(hint)
        return "\n".join(context)

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: List[Dict[str, Any]] | None = None,
        turn_id: str = "",
    ) -> None:
        """Submit persisted turn evidence on MemoryManager's background thread."""
        if not self.is_available():
            logger.debug("Harso turn skipped: provider unavailable")
            return
        if not messages:
            logger.debug("Harso turn skipped: no durable messages")
            return

        # Flush stamps _row_id before dispatch; never invent refs from text.
        user = assistant = None
        user_index = assistant_index = -1
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if type(message.get("_row_id")) is not int:
                continue
            if message.get("role") == "user" and user is None:
                user, user_index = message, index
            elif message.get("role") == "assistant" and assistant is None:
                assistant, assistant_index = message, index
            if user is not None and assistant is not None:
                break
        if user is None or assistant is None or assistant_index <= user_index:
            logger.debug("Harso turn skipped: no durable current-turn pair")
            return

        finalized_items = []
        for message in (user, assistant):
            content = flatten_message_text(message.get("content")).strip()
            if not content:
                logger.debug("Harso turn skipped: empty persisted content")
                return
            finalized_items.append({
                "role": message["role"],
                "native_item_ref": f"message:{message['_row_id']}",
                "content": content[:65536],
            })
        payload = {
            **self._scope(session_id),
            "current_user_ref": finalized_items[0]["native_item_ref"],
            "current_assistant_ref": finalized_items[1]["native_item_ref"],
            "finalized_items": finalized_items,
        }
        # WEV-1850: the turn's native identity (the caller's external_request_id
        # when a native submit named the turn). Forwarded verbatim — never minted
        # here. A turn without one sends no key, so the Ledger's handoff-objective
        # exclusion can never bind it to another turn.
        if turn_id:
            payload["native_turn_ref"] = turn_id
        response = self._post("/internal/harso/turns", payload)
        if response is None:
            raise HarsoWriteError("harso_turn_unacknowledged")
        logger.debug("Harso turn disposition: %s", response.get("disposition"))

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Dict[str, Any] | None = None,
    ) -> bool:
        metadata = metadata or {}
        operation_id, revision = metadata.get("operation_id"), metadata.get("revision")
        if (
            not isinstance(operation_id, str)
            or not operation_id
            or type(revision) is not int
        ):
            raise HarsoWriteError("harso_write_unacknowledged")
        response = self._post(
            "/internal/harso/mutations",
            {
                **self._scope(self._session_id),
                "action": action,
                "target": target,
                "content": content,
                "operation_id": operation_id,
                "revision": revision,
            },
        )
        if (
            response
            and response.get("acknowledged") is True
            and response.get("operation_id") == operation_id
            and response.get("revision") == revision
        ):
            return True
        raise HarsoWriteError("harso_write_unacknowledged")
