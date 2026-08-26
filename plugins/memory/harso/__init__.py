"""Harso's direct, scope-bound Hermes memory provider."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)
_TIMEOUT_SECONDS = 5
_MAX_CONTEXT_ITEMS = 5
_MAX_CONTEXT_TEXT = 1200


class HarsoMemoryProvider(MemoryProvider):
    """Use the private Weave API as the Harso admission boundary."""

    def __init__(self) -> None:
        self._endpoint = os.environ.get("WEAVE_HARSO_ENDPOINT", "").rstrip("/")
        self._profile_id = os.environ.get("WEAVE_HARSO_PROFILE_ID", "")
        self._profile_revision_id = os.environ.get("WEAVE_HARSO_PROFILE_REVISION_ID", "")
        self._bearer = os.environ.get("WEAVE_API_MCP_BEARER", "")
        self._route_key = os.environ.get("API_SERVER_KEY", "")
        self._session_id = ""

    @property
    def name(self) -> str:
        return "harso"

    def is_available(self) -> bool:
        return all((self._endpoint, self._profile_id, self._profile_revision_id,
                    self._bearer, self._route_key))

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
        response = self._post("/internal/harso/context", {
            **self._scope(session_id), "query": query,
        })
        if not response or response.get("degraded") is True:
            return ""
        items = response.get("items")
        if not isinstance(items, list):
            return ""
        context = []
        for item in items[:_MAX_CONTEXT_ITEMS]:
            if not isinstance(item, dict):
                continue
            citation, text = item.get("citation"), item.get("text")
            if isinstance(citation, str) and isinstance(text, str):
                context.append(f"{citation} {text[:_MAX_CONTEXT_TEXT]}")
        return "\n".join(context)

    def on_memory_write(
        self, action: str, target: str, content: str, metadata: Dict[str, Any] | None = None,
    ) -> bool:
        metadata = metadata or {}
        operation_id, revision = metadata.get("operation_id"), metadata.get("revision")
        if not isinstance(operation_id, str) or not operation_id or type(revision) is not int:
            return False
        response = self._post("/internal/harso/mutations", {
            **self._scope(self._session_id),
            "action": action,
            "target": target,
            "content": content,
            "operation_id": operation_id,
            "revision": revision,
        })
        return bool(
            response
            and response.get("acknowledged") is True
            and response.get("operation_id") == operation_id
            and response.get("revision") == revision
        )
