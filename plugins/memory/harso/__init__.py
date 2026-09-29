"""Harso's direct, scope-bound Hermes memory provider."""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List

from agent.message_content import flatten_message_text
from agent.memory_provider import MemoryProvider
from agent.secret_scope import get_secret

logger = logging.getLogger(__name__)
_TIMEOUT_SECONDS = 5
# Prefetch-only runtime settings (config.yaml ``plugins.harso``). The socket
# timeout stays below MemoryManager's caller-wait bound so a slow server frees
# the prefetch thread soon after the turn stops waiting; writes keep 5s.
_PREFETCH_TIMEOUT_SECONDS = 0.8
# Both finite bounds below admit the LARGEST response the server guarantees,
# so they never cut valid recall; the server's pack decides recall. Maxima,
# each enforced by weave-cloud on this path (not merely schema-declared):
#   entries  <= 256: harso-memory context.py _cap_entries caps the final pack
#               (main + Jev extras + pack-check) at CONTEXT_ENTRIES_MAX and
#               reports the rest as budget-excluded.
#   content  <= 4 * 32768 = 131072 UTF-8 bytes in total: TOTAL_TOKENS_MAX
#               counts ceil(content bytes / 4) per entry; a str has no more
#               chars than UTF-8 bytes.
#   per item (weave-api memory_service._recall_item, else recall fails closed
#               or the field is omitted): evidence_id <= 54 chars
#               ("memory-projection:" + UUID); <= 64 citations, each exactly
#               45 ("evidence:" + UUIDv7); occurred_at exactly 27
#               ("YYYY-MM-DDTHH:MM:SS.ffffffZ"); session_id exactly 42
#               ("weave-" + UUID); kind "projection"/"evidence".
#   envelope: <= 5 allowlisted gap reasons; routing hint <= 200 chars.
# Transport (JSON bytes; json.dumps default ", "/": " separators, above
# Starlette's compact ones): content worst escape is 6 bytes per content byte
# (a control char -> \u00XX) = 786432; per item, all fields but "text" =
# 3425, plus 255 ", " item separators; envelope with 5 gaps and a 200-char
# hint of worst-escaped chars = 1458.
# 786432 + 256 * 3425 + 255 * 2 + 1458 = 1665200, so 2 MiB.
_PREFETCH_MAX_BYTES = 2 * 1024 * 1024
# Rendered chars, whole output: per item the date prefix "[YYYY-MM-DD HH:MM] "
# 19 + 64 refs * 45 + 63 separators + 1 space before text + 1 newline = 2964;
# 131072 + 256 * 2964 + gaps line 77 + 1 + hint 200 = 890134, so 1 MiB.
_CONTEXT_MAX_CHARS = 1024 * 1024
_GAP_REASONS = ("missing", "stale", "contradictory", "privacy-excluded", "budget-excluded")
_P = r"(?:0\.[0-9]{2}|1\.00)"
_ACTION = rf"external action: (?:likely|unlikely|unsure) \({_P}\)"
# WEV-1850: the fixed vocabulary weave-api's RoutingHint.line() generates. No
# free text can match, so nothing instruction-shaped reaches the chat model.
_ROUTING_HINT = re.compile(
    rf"Routing hint: (?:(?:inline|work) \({_P}\)(?:; {_ACTION})?|none; {_ACTION})")


class HarsoWriteError(RuntimeError):
    """Content-free failure that lets D4 record an unacknowledged mirror."""


def _prefetch_limits() -> tuple[float, int, int]:
    """Read ``plugins.harso.prefetch_timeout`` / ``prefetch_max_bytes`` /
    ``context_max_chars`` per call.

    An absent section or key is silent; a present but invalid one (including
    an explicit null) and an unreadable config warn, content-free, and use the
    defaults."""
    timeout, max_bytes = _PREFETCH_TIMEOUT_SECONDS, _PREFETCH_MAX_BYTES
    max_chars = _CONTEXT_MAX_CHARS
    defaults = timeout, max_bytes, max_chars
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
    except Exception:
        logger.warning("plugins.harso config unreadable; using defaults")
        return defaults
    if not isinstance(config, dict):
        logger.warning("plugins.harso config unreadable; using defaults")
        return defaults
    plugins = config.get("plugins")
    if not isinstance(plugins, dict) or "harso" not in plugins:
        return defaults
    section = plugins["harso"]
    if not isinstance(section, dict):
        logger.warning("plugins.harso invalid; using defaults")
        return defaults
    if "prefetch_timeout" in section:
        raw = section["prefetch_timeout"]
        if (not isinstance(raw, bool) and isinstance(raw, (int, float))
                and 0 < raw <= _TIMEOUT_SECONDS):
            timeout = float(raw)
        else:
            logger.warning("plugins.harso.prefetch_timeout invalid; using %.1fs", timeout)
    if "prefetch_max_bytes" in section:
        raw = section["prefetch_max_bytes"]
        if type(raw) is int and 1024 <= raw <= 16 * 1024 * 1024:
            max_bytes = raw
        else:
            logger.warning("plugins.harso.prefetch_max_bytes invalid; using %d", max_bytes)
    if "context_max_chars" in section:
        raw = section["context_max_chars"]
        if type(raw) is int and 1024 <= raw <= 16 * 1024 * 1024:
            max_chars = raw
        else:
            logger.warning("plugins.harso.context_max_chars invalid; using %d", max_chars)
    return timeout, max_bytes, max_chars


def _date_prefix(occurred_at: Any) -> str:
    """``[YYYY-MM-DD HH:MM] `` in UTC for an ISO-8601 string, else ``""``.

    Only a parsed datetime is rendered, never the raw string. The wire is UTC,
    so a zone-less value is read as UTC."""
    if not isinstance(occurred_at, str):
        return ""
    try:
        parsed = datetime.fromisoformat(occurred_at)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("[%Y-%m-%d %H:%M] ")
    except (ValueError, OverflowError):
        return ""


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

    def on_session_switch(self, new_session_id: str, **_kwargs: Any) -> None:
        # A cached gateway agent serves a new conversation: calls that fall
        # back to the bound session must name the current one, not the first.
        if new_session_id:
            self._session_id = new_session_id

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return []

    def _post(
        self,
        path: str,
        payload: Dict[str, Any],
        *,
        timeout: float = _TIMEOUT_SECONDS,
        max_bytes: int | None = None,
    ) -> Dict[str, Any] | None:
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
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if max_bytes is None:
                    body = response.read()
                else:
                    body = response.read(max_bytes + 1)
                    if len(body) > max_bytes:
                        logger.warning("Harso response exceeded %d bytes; dropped", max_bytes)
                        return None
                payload = json.loads(body.decode("utf-8"))
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
        timeout, max_bytes, max_chars = _prefetch_limits()
        response = self._post(
            "/internal/harso/context",
            {
                **self._scope(session_id),
                "query": query,
            },
            timeout=timeout,
            max_bytes=max_bytes,
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
        lines = []
        for item in items:
            if not isinstance(item, dict):
                continue
            citation, text = item.get("citation"), item.get("text")
            citations = item.get("citations")
            # Wire-contract ceiling per entry, not a recall-breadth tuning knob.
            if (isinstance(citations, list) and citations
                    and all(isinstance(ref, str) and ref.strip() for ref in citations)):
                citation = " ".join(citations[:64])
            if (isinstance(citation, str) and citation.strip()
                    and isinstance(text, str) and text.strip()):
                lines.append(f"{_date_prefix(item.get('occurred_at'))}{citation} {text}")
        gaps = response.get("gaps")
        reasons = list(dict.fromkeys(
            gap["reason"] for gap in gaps
            if isinstance(gap, dict) and gap.get("reason") in _GAP_REASONS
        ))[:5] if isinstance(gaps, list) else []

        def render(kept: List[str], excluded: bool) -> str:
            parts = list(kept)
            # Gaps may annotate admitted evidence, never create context by
            # themselves; the hint is independent of memory admission.
            if lines:
                shown = reasons + ["budget-excluded"] if (
                    excluded and "budget-excluded" not in reasons) else reasons
                if shown:
                    parts.append("Memory gaps: " + ", ".join(shown))
            if hint:
                parts.append(hint)
            return "\n".join(parts)

        # context_max_chars bounds the COMPLETE output, gaps line and hint
        # included. Over it, keep a whole-item prefix in server order under the
        # room left by the final suffix; never cut an item. The suffix is at
        # most 278 chars, under the 1024 minimum, so an admitted item excluded
        # by the bound is always reported as budget-excluded, even if none fit.
        output = render(lines, False)
        if len(output) <= max_chars:
            return output
        used, kept = len(render([], True)), []
        for line in lines:
            if used + len(line) + 1 > max_chars:
                break
            used += len(line) + 1
            kept.append(line)
        return render(kept, True)

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
