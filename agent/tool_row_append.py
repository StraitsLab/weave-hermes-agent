"""Durable appends to an already-saved tool result row.

Three places append user/system-origin text to the newest ``role:"tool"``
message instead of inserting a new message (role alternation and the prompt
cache prefix survive that way):

* the post-batch /steer drain (``apply_pending_steer_to_tool_results``);
* the pre-API /steer drain (``conversation_loop``);
* the one-shot run-budget wrap-up notice (``conversation_loop``).

The incremental flush persists a tool row right after it is appended and
every later flush skips rows carrying the persisted marker, so an append made
after that flush never reached state.db: every reload (gateway native submit,
resume, branch) replayed the tool row WITHOUT the steer. ``append_to_tool_row``
is the one helper all three sites use. It updates the in-memory row exactly
as before and, when the row is already durable, rewrites it through a guarded
``SessionDB.append_to_tool_message``. When the row cannot be made durable the
append is refused (nothing changes) so the caller can re-queue the steer.

Steer identification for pruning/compaction: each append records its span
(kind + exact length) in the row's ``display_metadata["tool_appends"]``,
written in the same guarded update as the content (no schema change; the
column already exists and is never sent to a provider). Rows without records
fall back to the steer marker text, conservatively. See
``split_trailing_appends``.
"""

from __future__ import annotations

import logging
import re
from typing import Any, List, Optional, Tuple

from agent.prompt_builder import STEER_MARKER_CLOSE, STEER_MARKER_OPEN

logger = logging.getLogger(__name__)

# Same key run_agent / context_compressor use; duplicated (not imported) to
# keep this module free of the run_agent import cycle.
_DB_PERSISTED_MARKER = "_db_persisted"

# One-shot wall-clock wrap-up notice (re-exported by agent.conversation_loop).
RUN_BUDGET_WRAPUP_NOTICE = (
    "[SYSTEM NOTICE — run time budget nearly exhausted] "
    "Run time budget nearly exhausted. Stop new discovery/verification work "
    "now. Produce the required final deliverable (answer/JSON/summary) from "
    "the state you already have, completing only mandatory writes."
)

_STEER_BLOCK_OPEN = STEER_MARKER_OPEN + "\n"
_STEER_BLOCK_CLOSE = "\n" + STEER_MARKER_CLOSE


# ---------------------------------------------------------------------------
# Persisted shape of a message's content (single owner for flush + guard)
# ---------------------------------------------------------------------------


def persisted_message_content(role: Any, content: Any) -> Any:
    """Return the ``content`` value the session flush writes for a row.

    * ``{_multimodal: True}`` envelopes persist as their text summary.
    * Tool rows keep list content structurally (text + image blocks keep
      their ``type``); ``SessionDB._encode_content`` stores it as JSON and
      ``_decode_content`` restores the list on reload, so a replayed tool row
      is the same content the live request sent — including any appended
      steer block.
    * Other roles keep the historical text flattening (images become
      ``[screenshot]``).
    """
    from agent.tool_dispatch_helpers import (
        _is_multimodal_tool_result,
        _multimodal_text_summary,
    )

    if _is_multimodal_tool_result(content):
        return _multimodal_text_summary(content)
    if isinstance(content, list) and role != "tool":
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(str(p.get("text", "")))
            elif isinstance(p, dict) and p.get("type") in {"image", "image_url", "input_image"}:
                parts.append("[screenshot]")
        return "\n".join(parts) if parts else None
    return content


# ---------------------------------------------------------------------------
# The append rule + durable write
# ---------------------------------------------------------------------------


def _appended_content(existing: Any, suffix: str) -> Any:
    """string + text -> string; block list + text -> list + one text block."""
    if existing is None:
        existing = ""
    if isinstance(existing, str):
        return existing + suffix
    if isinstance(existing, list):
        blocks = list(existing)
        blocks.append({"type": "text", "text": suffix.lstrip()})
        return blocks
    return None


def _log_refusal(agent: Any, reason: str, row_id: Any) -> None:
    if not getattr(agent, "_tool_append_refusal_logged", False):
        try:
            agent._tool_append_refusal_logged = True
        except Exception:
            pass
        logger.warning(
            "Tool-row append not durable (%s, row_id=%s); text re-queued instead "
            "of being delivered through a row the transcript cannot replay",
            reason,
            row_id,
        )
    else:
        logger.debug("Tool-row append refused again (%s, row_id=%s)", reason, row_id)


def append_to_tool_row(agent: Any, msg: dict, suffix: str) -> bool:
    """Append ``suffix`` to tool message ``msg`` and make it durable.

    Returns True when the text was delivered (in memory, and durably when the
    row is already saved). Returns False — with ``msg`` untouched — when the
    append cannot be persisted; the caller re-queues it. Never raises.

    * Row not yet flushed (no persisted marker): in-memory append only; the
      next flush writes the row including the appended text.
    * Row flushed with a ``_row_id``: guarded ``UPDATE`` that only lands if
      the stored row still holds exactly the pre-append content.
    * Row flushed without a ``_row_id`` (e.g. replayed history): refuse.
    """
    try:
        existing = msg.get("content", "")
        new_content = _appended_content(existing, suffix)
        if new_content is None:
            _log_refusal(agent, "unsupported content shape", msg.get("_row_id"))
            return False
        record = _append_record(existing, suffix)
        if msg.get(_DB_PERSISTED_MARKER):
            db = getattr(agent, "_session_db", None)
            row_id = msg.get("_row_id")
            if db is not None:
                if not isinstance(row_id, int):
                    _log_refusal(agent, "saved row has no row id", row_id)
                    return False
                role = msg.get("role", "tool")
                updated = db.append_to_tool_message(
                    getattr(agent, "session_id", None),
                    row_id,
                    msg.get("tool_call_id"),
                    persisted_message_content(role, existing),
                    persisted_message_content(role, new_content),
                    append_record=record,
                )
                if updated != 1:
                    _log_refusal(agent, "stored row changed", row_id)
                    return False
        msg["content"] = new_content
        if record is not None:
            # Span record for pruning/compaction (the flush writes it for an
            # unsaved row; the guarded update above already stored it).
            msg["display_metadata"] = _with_append_record(msg.get("display_metadata"), record)
        return True
    except Exception as exc:  # a durability failure must never crash the turn
        _log_refusal(agent, f"error: {exc}", msg.get("_row_id") if isinstance(msg, dict) else None)
        return False


def requeue_steer(agent: Any, steer_text: str) -> None:
    """Put drained steer text back in front of any newer pending steer."""
    lock = getattr(agent, "_pending_steer_lock", None)

    def _merge():
        pending = getattr(agent, "_pending_steer", None)
        agent._pending_steer = (steer_text + "\n" + pending) if pending else steer_text

    if lock is not None:
        with lock:
            _merge()
    else:
        _merge()


# ---------------------------------------------------------------------------
# Pruning / compaction: ONE split/reattach authority for appended parts
# ---------------------------------------------------------------------------
#
# Every rewrite of tool content (prune, pressure/lean demotion, last-resort
# salvage, both compaction serializers, final summarizer-input budgeting)
# goes through ``split_tool_message`` / ``split_trailing_appends`` to peel
# the appended parts off the ORIGINAL (possibly structured) content, rewrites
# or bounds only the tool body, and re-attaches the protected steer pieces
# unclipped (``with_steers``). The run-budget wrap-up notice is disposable.
#
# Identification is unambiguous for every append made through
# ``append_to_tool_row``: it records a span per append in the row's
# ``display_metadata["tool_appends"]`` (kind + exact character length),
# persisted with the content in the same guarded write. Content without
# records (rows appended before this existed) falls back to the marker text,
# conservatively: an opening marker quoted inside a steer never becomes the
# boundary (the protected span grows to the outer marker instead of cutting
# the user's prefix).

_APPENDS_META_KEY = "tool_appends"
_STEER_PIECE_OPEN = "\n\n" + _STEER_BLOCK_OPEN
_NOTICE_PIECE = "\n\n" + RUN_BUDGET_WRAPUP_NOTICE

# Serialized-summary spans: after ``defang_steer_markers`` has neutralized
# every marker in ordinary material, each OPEN..CLOSE block in a serialized
# summary input IS a protected steer (see ``protected_steer_spans``).
_QUOTED_OPEN = STEER_MARKER_OPEN.replace("[OUT-OF-BAND", "[quoted OUT-OF-BAND", 1)
_QUOTED_CLOSE = STEER_MARKER_CLOSE.replace("[/OUT-OF-BAND", "[/quoted OUT-OF-BAND", 1)
_PROTECTED_SPAN_RE = re.compile(
    re.escape(STEER_MARKER_OPEN) + r"\n.*?\n" + re.escape(STEER_MARKER_CLOSE),
    re.S,
)


def _append_kind(suffix: str) -> Optional[str]:
    if suffix == _NOTICE_PIECE:
        return "notice"
    if _is_steer_piece(suffix):
        return "steer"
    return None


def _append_record(existing: Any, suffix: str) -> Optional[dict]:
    kind = _append_kind(suffix)
    if kind is None:
        return None
    piece = suffix if isinstance(existing, str) or existing is None else suffix.lstrip()
    return {"kind": kind, "chars": len(piece)}


def _with_append_record(metadata: Any, record: Optional[dict]) -> Any:
    if record is None:
        return metadata
    meta = dict(metadata) if isinstance(metadata, dict) else {}
    records = meta.get(_APPENDS_META_KEY)
    meta[_APPENDS_META_KEY] = (list(records) if isinstance(records, list) else []) + [record]
    return meta


def _is_steer_piece(piece: str) -> bool:
    return (
        len(piece) >= len(_STEER_PIECE_OPEN) + len(_STEER_BLOCK_CLOSE)
        and piece.startswith(_STEER_PIECE_OPEN)
        and piece.endswith(_STEER_BLOCK_CLOSE)
    )


def _is_steer_block_text(text: str) -> bool:
    return _is_steer_piece("\n\n" + text.lstrip())


def _append_records(metadata: Any) -> List[Tuple[str, int]]:
    if not isinstance(metadata, dict):
        return []
    raw = metadata.get(_APPENDS_META_KEY)
    out: List[Tuple[str, int]] = []
    if isinstance(raw, list):
        for rec in raw:
            if (
                isinstance(rec, dict)
                and rec.get("kind") in ("steer", "notice")
                and isinstance(rec.get("chars"), int)
                and rec["chars"] > 0
            ):
                out.append((rec["kind"], rec["chars"]))
    return out


def _peel_string_by_markers(body: str, steers: List[str]) -> str:
    """Legacy (record-less) peeling; ambiguity keeps MORE, never less."""
    while True:
        if body.endswith(_NOTICE_PIECE):
            body = body[: -len(_NOTICE_PIECE)]
            continue
        if body.endswith(_STEER_BLOCK_CLOSE):
            start = body.rfind(_STEER_PIECE_OPEN)
            if start >= 0:
                # An earlier opening marker with no closing marker between it
                # and ``start`` means ``start`` may be a marker QUOTED inside
                # the user's steer: grow the protected span to the outer one.
                while True:
                    prev = body.rfind(_STEER_PIECE_OPEN, 0, start)
                    if prev < 0:
                        break
                    if body.find(_STEER_BLOCK_CLOSE, prev + len(_STEER_PIECE_OPEN), start) >= 0:
                        break
                    start = prev
                steers.insert(0, body[start:])
                body = body[:start]
                continue
        return body


def split_trailing_appends(content: Any, metadata: Any = None) -> Tuple[Any, List[str]]:
    """Peel appended steer / wrap-up parts off the END of tool content.

    Returns ``(body, steers)``: ``body`` is the tool output (same shape as
    ``content``: a string, or a block list with the appended text blocks
    removed) and ``steers`` the protected steer pieces in original order,
    each in string-append form (``"\\n\\n" + OPEN + "\\n" + text + "\\n" +
    CLOSE``) so callers re-attach them verbatim with ``with_steers``. The
    wrap-up notice is dropped (disposable). ``metadata`` is the row's
    ``display_metadata``; its ``tool_appends`` records identify each span
    exactly. Only the tail is examined: a lookalike marker in the middle of
    a tool output is ordinary output.
    """
    steers: List[str] = []
    records = _append_records(metadata)
    if isinstance(content, str):
        body = content
        for kind, n in reversed(records):
            piece = body[-n:] if n <= len(body) else None
            if kind == "notice":
                if piece == _NOTICE_PIECE:
                    body = body[:-n]
                continue  # a disposable notice already dropped by a rewrite
            if piece is None or not _is_steer_piece(piece):
                break  # records no longer describe this content: fall back
            steers.insert(0, piece)
            body = body[:-n]
        return _peel_string_by_markers(body, steers), steers
    if isinstance(content, list):
        blocks = list(content)

        def _last_text() -> Optional[str]:
            if not blocks:
                return None
            last = blocks[-1]
            if isinstance(last, dict) and last.get("type") == "text":
                text = last.get("text")
                return text if isinstance(text, str) else None
            return None

        for kind, n in reversed(records):
            text = _last_text()
            if kind == "notice":
                if text == RUN_BUDGET_WRAPUP_NOTICE:
                    blocks.pop()
                continue
            if text is None or len(text) != n or not _is_steer_block_text(text):
                break
            steers.insert(0, "\n\n" + text.lstrip())
            blocks.pop()
        while True:
            text = _last_text()
            if text is None:
                break
            if text == RUN_BUDGET_WRAPUP_NOTICE:
                blocks.pop()
                continue
            if _is_steer_block_text(text):
                steers.insert(0, "\n\n" + text.lstrip())
                blocks.pop()
                continue
            break
        return blocks, steers
    return content, steers


def split_tool_message(msg: Any) -> Tuple[Any, List[str]]:
    """``split_trailing_appends`` for a message, using its persisted spans."""
    if not isinstance(msg, dict):
        return msg, []
    return split_trailing_appends(msg.get("content"), msg.get("display_metadata"))


def steer_texts(steers: List[str]) -> List[str]:
    """The user's words inside each steer piece (for summaries)."""
    out = []
    for s in steers:
        t = s[2:] if s.startswith("\n\n") else s
        if t.startswith(_STEER_BLOCK_OPEN) and t.endswith(_STEER_BLOCK_CLOSE) and len(t) >= len(_STEER_BLOCK_OPEN) + len(_STEER_BLOCK_CLOSE):
            t = t[len(_STEER_BLOCK_OPEN): len(t) - len(_STEER_BLOCK_CLOSE)]
        out.append(t)
    return out


def with_steers(new_text: str, steers: Optional[List[str]]) -> str:
    """Re-attach preserved steer pieces after a rewritten tool body."""
    return new_text + "".join(steers or [])


def defang_steer_markers(text: str) -> str:
    """Neutralize steer markers in ORDINARY summarizer-input material.

    A serialized summarizer input must carry exactly one kind of
    OPEN..CLOSE block: a genuine protected steer (``summary_steer_piece``).
    Tool output, user/assistant text or a steer that QUOTES the marker is
    rewritten to a visibly-quoted form, so input budgeting can protect
    steer spans by position without trusting lookalikes.
    """
    if not text:
        return text
    return text.replace(STEER_MARKER_OPEN, _QUOTED_OPEN).replace(STEER_MARKER_CLOSE, _QUOTED_CLOSE)


def summary_steer_piece(piece: str, redact) -> str:
    """Render one protected steer for summarizer input: redacted, unclipped."""
    inner = steer_texts([piece])[0]
    return _STEER_PIECE_OPEN + defang_steer_markers(redact(inner)) + _STEER_BLOCK_CLOSE


def protected_steer_spans(text: str) -> List[Tuple[int, int]]:
    """``(start, end)`` of every protected steer block in a serialized input."""
    if not text or STEER_MARKER_OPEN not in text:
        return []
    return [(m.start(), m.end()) for m in _PROTECTED_SPAN_RE.finditer(text)]
