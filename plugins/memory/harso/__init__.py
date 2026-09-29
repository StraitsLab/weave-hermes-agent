"""Harso's direct, scope-bound Hermes memory provider."""

from __future__ import annotations

import contextvars
from collections import OrderedDict
import hashlib
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

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
# Always-on profile + memory tools (config.yaml ``plugins.harso``).
_PROFILE_WAIT_SECONDS = 1.0
_PROFILE_TIMEOUT_SECONDS = 3
_TOOL_TIMEOUT_SECONDS = 8
_PROFILE_HEADER = "Profile (always-on memory):"
# Bound on per-session profile cache entries held by one (cached) agent.
_PROFILE_CACHE_SESSIONS = 64
# Process-wide warm-up fetch pool: at most this many worker threads for ALL
# providers and sessions (a trickling server cannot pile up daemons). Waiting
# jobs run newest first, so the chat that just opened is never starved.
_PROFILE_FETCH_MAX = 8
_PROFILE_PENDING_MAX = 64
_FETCH_LOCK = threading.Lock()
_FETCH_PENDING: "OrderedDict[Tuple[str, ...], Any]" = OrderedDict()
_FETCH_WORKERS = 0


def _fetch_worker() -> None:
    global _FETCH_WORKERS
    while True:
        with _FETCH_LOCK:
            if not _FETCH_PENDING:
                _FETCH_WORKERS -= 1
                return
            _key, job = _FETCH_PENDING.popitem(last=True)
        try:
            job(False)
        except Exception:  # a job never raises; the pool must outlive one that does
            pass


def _schedule_fetch(key: Tuple[str, ...], job: Any) -> None:
    """Queue ``job(skip)``; start a worker if under the process-wide cap.
    Every job leaves the queue by exactly one path: a worker runs it, or it
    is displaced (same key, or oldest past the pending cap) and run with
    skip=True (cleanup only), so no waiter hangs."""
    global _FETCH_WORKERS
    dropped = []
    with _FETCH_LOCK:
        replaced = _FETCH_PENDING.pop(key, None)
        if replaced is not None:
            dropped.append(replaced)
        _FETCH_PENDING[key] = job
        if len(_FETCH_PENDING) > _PROFILE_PENDING_MAX:
            dropped.append(_FETCH_PENDING.popitem(last=False)[1])
        start = _FETCH_WORKERS < _PROFILE_FETCH_MAX
        if start:
            _FETCH_WORKERS += 1
    for stale in dropped:
        stale(True)
    if start:
        try:
            threading.Thread(target=_fetch_worker, daemon=True, name="harso-profile").start()
        except Exception:
            with _FETCH_LOCK:
                _FETCH_WORKERS -= 1
                orphan = _FETCH_PENDING.pop(key, None)
            if orphan is not None:
                orphan(True)
            raise
# Readiness answers are tiny; bound the read so a trickling body cannot pin a thread.
_READINESS_MAX_BYTES = 4096
_FENCE_OPEN, _FENCE_CLOSE = "<memory-data untrusted>", "</memory-data>"


def _fenced(text: str) -> bool:
    """Exactly one server fence around the whole text (weave-api memory_delivery.face_envelope)."""
    return (text.startswith(_FENCE_OPEN) and text.endswith(_FENCE_CLOSE)
            and text.count(_FENCE_OPEN) == 1 and text.count(_FENCE_CLOSE) == 1)
_TOOL_UNAVAILABLE = json.dumps({"error": "memory unavailable"})
# tool name -> (wire action, argument key or None)
_TOOL_ACTIONS = {
    "memory_profile": ("profile", None),
    "memory_search": ("search", "query"),
    "memory_open": ("open", "ref"),
}
_TOOL_SCHEMAS = [
    {
        "name": "memory_profile",
        "description": "The user's current always-on profile.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "memory_search",
        "description": (
            "Search the user's long-term memory (facts, preferences, past "
            "conversations). Use before saying you don't know something about "
            "the user."),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What to look for."}},
            "required": ["query"],
        },
    },
    {
        "name": "memory_open",
        "description": "Open one memory ref from memory_search to see its full text and sources.",
        "parameters": {
            "type": "object",
            "properties": {"ref": {"type": "string", "description": "A ref from memory_search."}},
            "required": ["ref"],
        },
    },
]


def _render_recall(response: Any, max_chars: int, profile: str = "") -> str:
    """Render a /internal/harso/context response within ``max_chars``.

    A recall item whose text already appears verbatim in ``profile`` is
    skipped; with no profile this is exactly the base prefetch rendering."""
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
            if profile and text.strip() in profile:
                continue  # already shown verbatim in the always-on profile
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


def _feature_settings() -> Tuple[bool, bool, float]:
    """Read ``plugins.harso.tools_enabled`` / ``profile_enabled`` /
    ``profile_wait`` per call.

    Absent keys are silent; an invalid key warns content-free and uses its
    default. An unreadable config or malformed section silently uses the
    defaults here: ``_prefetch_limits`` already reports those."""
    tools, profile, wait = True, True, _PROFILE_WAIT_SECONDS
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
    except Exception:
        return tools, profile, wait
    plugins = config.get("plugins") if isinstance(config, dict) else None
    section = plugins.get("harso") if isinstance(plugins, dict) else None
    if not isinstance(section, dict):
        return tools, profile, wait
    for key in ("tools_enabled", "profile_enabled"):
        if key in section:
            if isinstance(section[key], bool):
                if key == "tools_enabled":
                    tools = section[key]
                else:
                    profile = section[key]
            else:
                logger.warning("plugins.harso.%s invalid; using true", key)
    if "profile_wait" in section:
        raw = section["profile_wait"]
        if (not isinstance(raw, bool) and isinstance(raw, (int, float))
                and 0 <= raw <= _TIMEOUT_SECONDS):
            wait = float(raw)
        else:
            logger.warning("plugins.harso.profile_wait invalid; using %.1fs", wait)
    return tools, profile, wait


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
        # session id -> profile text ("" once a fetch completed without one).
        self._profile: Dict[str, str] = {}
        # session id -> Event set when the in-flight profile fetch finishes.
        self._profile_inflight: Dict[str, threading.Event] = {}
        self._profile_lock = threading.Lock()

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
        self._start_profile_fetch(session_id)

    def on_session_switch(self, new_session_id: str, **_kwargs: Any) -> None:
        # A cached gateway agent serves a new conversation: calls that fall
        # back to the bound session must name the current one, not the first.
        if new_session_id:
            self._session_id = new_session_id
            self._start_profile_fetch(new_session_id)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        # After each turn: refresh the always-on profile for the next one.
        self._start_profile_fetch(session_id or self._session_id)

    # -- Always-on profile ----------------------------------------------------

    def _profile_key(self, session_id: str) -> Tuple[str, ...]:
        """Cache key bound to the CURRENT secret scope: a cached agent reused
        under another profile (multiplex) or revision never sees this entry."""
        route = hashlib.sha256(self._route_key.encode("utf-8")).hexdigest()
        return (self._endpoint, self._profile_id, self._profile_revision_id, route, session_id)

    def _start_profile_fetch(self, session_id: str) -> None:
        """Warm admission and refresh the profile on a daemon thread.

        Never raises into the caller; at most one fetch per session in flight."""
        try:
            if not session_id or not _feature_settings()[1] or not self.is_available():
                return
            key = self._profile_key(session_id)
            with self._profile_lock:
                if key in self._profile_inflight:
                    return
                done = threading.Event()
                self._profile_inflight[key] = done
            # Copy the caller's context: multiplex secrets live in a ContextVar.
            context = contextvars.copy_context()

            def job(skip: bool) -> None:
                context.run(self._fetch_profile, session_id, key, done, skip)

            # F3: the process-wide pool bounds threads; newest job runs first.
            _schedule_fetch(key, job)
        except Exception as exc:
            logger.warning("Harso profile fetch not started: %s", type(exc).__name__)

    def _fetch_profile(self, session_id: str, key: Tuple[str, ...], done: threading.Event,
                       skip: bool = False) -> None:
        text = None
        try:
            if skip:  # evicted from the pending queue: release waiters only
                return
            scope = self._scope(session_id)
            # Warm the server's admission cache before turn 1's recall.
            self._post("/internal/harso/readiness", dict(scope),
                       timeout=_PROFILE_TIMEOUT_SECONDS, max_bytes=_READINESS_MAX_BYTES)
            response = self._post(
                "/internal/harso/memory-tool",
                {**scope, "action": "profile", "argument": ""},
                timeout=_PROFILE_TIMEOUT_SECONDS,
                max_bytes=_prefetch_limits()[1],
            )
            if response is not None:
                found, body = response.get("found"), response.get("text")
                # F2: only the server's fenced envelope is ever injected.
                body = body.strip() if isinstance(body, str) else ""
                text = body if (found is True and _fenced(body)) else ""
        except Exception as exc:
            logger.warning("Harso profile fetch failed: %s", type(exc).__name__)
        finally:
            with self._profile_lock:
                # A failed refresh keeps the last good profile; a completed
                # first fetch always records a value so later turns never wait.
                if text is not None or key not in self._profile:
                    self._profile.pop(key, None)
                    self._profile[key] = text or ""
                    while len(self._profile) > _PROFILE_CACHE_SESSIONS:
                        self._profile.pop(next(iter(self._profile)))
                if self._profile_inflight.get(key) is done:
                    self._profile_inflight.pop(key, None)
            done.set()

    def _cached_profile(self, session_id: str, deadline: float) -> str:
        """The cached profile; before the first fetch lands, wait until
        ``deadline`` for it, then continue without."""
        key = self._profile_key(session_id)
        with self._profile_lock:
            text = self._profile.get(key)
            pending = self._profile_inflight.get(key)
        if text is None and pending is not None:
            pending.wait(max(0.0, deadline - time.monotonic()))
            with self._profile_lock:
                text = self._profile.get(key)
        return text if text and _fenced(text) else ""

    # -- Tools ----------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        if not _feature_settings()[0]:
            return []
        return json.loads(json.dumps(_TOOL_SCHEMAS))

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **_kwargs: Any) -> str:
        """POST one memory-tool action; always a JSON string, never raises."""
        try:
            if tool_name not in _TOOL_ACTIONS or not _feature_settings()[0]:
                return _TOOL_UNAVAILABLE
            action, key = _TOOL_ACTIONS[tool_name]
            argument = ""
            if key is not None:
                argument = args.get(key) if isinstance(args, dict) else None
                if not isinstance(argument, str) or not argument.strip():
                    return json.dumps({"error": f"{key} is required"})
                argument = argument.strip()
            session_id = self._session_id
            if not session_id or not self.is_available():
                return _TOOL_UNAVAILABLE
            response = self._post(
                "/internal/harso/memory-tool",
                {**self._scope(session_id), "action": action, "argument": argument},
                timeout=_TOOL_TIMEOUT_SECONDS,
                max_bytes=_prefetch_limits()[1],
            )
            if not isinstance(response, dict):
                return _TOOL_UNAVAILABLE
            out = json.dumps(response)
            # F4: the SERIALIZED result is what reaches the model; bound it.
            if len(out) > min(_prefetch_limits()[2], _prefetch_limits()[1]):
                logger.warning("Harso memory tool result over cap; dropped")
                return json.dumps({"error": "memory result too large"})
            return out
        except Exception as exc:
            logger.warning("Harso memory tool failed: %s", type(exc).__name__)
            return _TOOL_UNAVAILABLE

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
            # Content-free: an HTTP reason or URL error text may echo headers.
            status = getattr(exc, "code", None)
            logger.warning("Harso request unavailable: %s%s", type(exc).__name__,
                           f" {status}" if isinstance(status, int) else "")
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
        _tools, profile_on, profile_wait = _feature_settings()
        # The profile wait overlaps the recall POST: at most wait + timeout.
        deadline = time.monotonic() + profile_wait
        response = self._post(
            "/internal/harso/context",
            {
                **self._scope(session_id),
                "query": query,
            },
            timeout=timeout,
            max_bytes=max_bytes,
        )
        if not profile_on:
            return _render_recall(response, max_chars)
        profile = self._cached_profile(session_id, deadline)
        if not profile:
            return _render_recall(response, max_chars)
        # The profile counts toward context_max_chars and is never cut: over
        # the cap by itself it drops. Recall then fits the room left, whole
        # items only; if not even its gaps/hint suffix fits, the profile wins.
        block = f"{_PROFILE_HEADER}\n{profile}"
        if len(block) > max_chars:
            return _render_recall(response, max_chars)
        room = max_chars - len(block) - 1
        recall = _render_recall(response, room, profile) if room > 0 else ""
        if not recall or len(recall) > room:
            return block
        return f"{block}\n{recall}"

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
