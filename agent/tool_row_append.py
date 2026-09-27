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
# Identification: every append made through ``append_to_tool_row`` records
# a span (kind + exact character length) in the row's
# ``display_metadata["tool_appends"]``, persisted with the content in the
# same guarded write. Records are AUTHORITATIVE: once a row has records, the
# text they do not cover is ordinary tool output and is never marker-parsed
# (a lookalike steer block printed by a tool stays tool output). Records that
# no longer describe the content are not second-guessed either: the whole
# unmatched tail is kept as protected text. Marker parsing runs ONLY for rows
# without records (appended before records existed), and there ambiguity
# keeps MORE, never less (see ``_peel_string_by_markers``).

_APPENDS_META_KEY = "tool_appends"
_STEER_PIECE_OPEN = "\n\n" + _STEER_BLOCK_OPEN
_NOTICE_PIECE = "\n\n" + RUN_BUDGET_WRAPUP_NOTICE

# Ordinary summarizer-input material (tool output, other messages, a previous
# summary) gets its markers rewritten to a visibly-quoted form. Protected
# steers are NOT defanged: their bytes reach the summarizer verbatim (only
# secret redaction applies) and their positions travel with the serialized
# text (``SummaryInput``), never re-found by pattern.
_QUOTED_OPEN = STEER_MARKER_OPEN.replace("[OUT-OF-BAND", "[quoted OUT-OF-BAND", 1)
_QUOTED_CLOSE = STEER_MARKER_CLOSE.replace("[/OUT-OF-BAND", "[/quoted OUT-OF-BAND", 1)


def _append_kind(suffix: str) -> Optional[str]:
    if suffix == _NOTICE_PIECE:
        return "notice"
    if _is_steer_piece(suffix):
        return "steer"
    return None


def _append_record(existing: Any, suffix: str) -> Optional[dict]:
    """Span record for one append (kind + exact length of the appended piece).

    Pre-existing content is never re-classified here: a row's text that no
    record covers is its tool output (a legacy record-less append on a row
    that later receives its first recorded append is indistinguishable from
    tool output ending in a lookalike block, so it is left as output).
    """
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


_INVALID_RECORDS = "invalid"


def _append_records(metadata: Any):
    """The row's span records: ``[]`` when it has none, ``"invalid"`` when
    records exist but are malformed (treated like records that no longer
    match: keep the tail), else ``[(kind, chars), ...]``."""
    if not isinstance(metadata, dict):
        return []
    raw = metadata.get(_APPENDS_META_KEY)
    if raw is None or raw == []:
        return []
    if not isinstance(raw, list):
        return _INVALID_RECORDS
    def _valid(rec: Any) -> bool:
        return (
            isinstance(rec, dict)
            and rec.get("kind") in ("steer", "notice")
            and isinstance(rec.get("chars"), int)
            and not isinstance(rec.get("chars"), bool)
            and rec["chars"] > 0
        )

    out: List[Tuple[str, int]] = []
    for rec in raw:
        if not _valid(rec):
            return _INVALID_RECORDS
        out.append((rec["kind"], rec["chars"]))
    return out


def _peel_string_by_markers(body: str, steers: List[str]) -> str:
    """Legacy (record-less) peeling; ambiguity keeps MORE, never less.

    A record-less row cannot tell a marker QUOTED inside a steer from the
    marker that opened it, and a closing marker between two openings proves
    nothing (the user may have quoted a whole block). So when the content
    ends with a closing marker, everything from the EARLIEST opening marker
    to the end is protected.

    That region is ATOMIC: it is returned as ONE piece and never split at an
    inner closing/opening pair. Two genuine successive legacy appends are
    byte-identical to one user steer that quotes ``CLOSE`` + ``OPEN``, so an
    inner marker can never be proven producer-owned; every consumer
    (pruning, ``steer_texts``, summaries, deterministic fallback) therefore
    unwraps only the region's outermost producer ``OPEN`` (first) and
    ``CLOSE`` (last) and keeps every inner byte, markers included, verbatim.
    Genuine successive legacy appends thus stay together as one piece
    (over-retention is the safe side; recorded rows split exactly).

    A wrap-up notice INSIDE the region is kept in place as text: without
    records it may be a user's quotation of the notice. Only notices at the
    very end of the row, after its last closing marker, are dropped: every
    steer block ends with a closing marker and the notice contains none, so
    those bytes cannot be steer text.
    """
    while body.endswith(_NOTICE_PIECE):
        body = body[: -len(_NOTICE_PIECE)]
    if not body.endswith(_STEER_BLOCK_CLOSE):
        return body
    start = body.find(_STEER_PIECE_OPEN)
    if start < 0:
        return body
    steers.insert(0, body[start:])
    return body[:start]


def split_trailing_appends(content: Any, metadata: Any = None) -> Tuple[Any, List[str]]:
    """Peel appended steer / wrap-up parts off the END of tool content.

    Returns ``(body, steers)``: ``body`` is the tool output (same shape as
    ``content``: a string, or a block list with the appended text blocks
    removed) and ``steers`` the protected steer pieces in original order,
    each in string-append form (``"\\n\\n" + OPEN + "\\n" + text + "\\n" +
    CLOSE``) so callers re-attach them verbatim with ``with_steers``. The
    wrap-up notice is dropped (disposable). ``metadata`` is the row's
    ``display_metadata``; its ``tool_appends`` records identify each span
    exactly and are authoritative (text they do not cover is ordinary
    output). Rows without records fall back to marker parsing, which keeps
    MORE on ambiguity, never less.
    """
    steers: List[str] = []
    records = _append_records(metadata)
    if isinstance(content, str):
        if not records:
            body = _peel_string_by_markers(content, steers)
            return body, steers
        body = content
        consumed = records != _INVALID_RECORDS
        for kind, n in reversed(records if consumed else []):
            piece = body[-n:] if n <= len(body) else None
            if kind == "notice":
                if piece == _NOTICE_PIECE:
                    body = body[:-n]
                continue  # a disposable notice already dropped by a rewrite
            if piece is None or not _is_steer_piece(piece):
                consumed = False
                break
            steers.insert(0, piece)
            body = body[:-n]
        if not consumed and body:
            # Records no longer describe this content: do not guess where
            # the tool output ends. Keep the whole unmatched tail protected.
            steers.insert(0, body)
            body = ""
        # Every record consumed: the rest is ordinary tool output (never
        # marker-parsed).
        return body, steers
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

        if not records:
            # Legacy (record-less) rows: each append was its own trailing
            # text block, so a quoted marker cannot split a steer here.
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
        consumed = records != _INVALID_RECORDS
        for kind, n in reversed(records if consumed else []):
            text = _last_text()
            if kind == "notice":
                if text == RUN_BUDGET_WRAPUP_NOTICE:
                    blocks.pop()
                continue
            if text is None or len(text) != n or not _is_steer_block_text(text):
                consumed = False
                break
            steers.insert(0, "\n\n" + text.lstrip())
            blocks.pop()
        if not consumed:
            # Appends are always trailing text blocks: keep the whole
            # trailing text run protected rather than guess.
            while True:
                text = _last_text()
                if text is None:
                    break
                steers.insert(0, "\n\n" + text)
                blocks.pop()
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
    """Neutralize steer markers in ORDINARY summarizer-input material only.

    Tool output, user/assistant text and a previous summary are rewritten to
    a visibly-quoted form so the summarizer never mistakes them for a live
    steer. Never applied to protected steer text (``summary_steer_piece``).
    """
    if not text:
        return text
    return text.replace(STEER_MARKER_OPEN, _QUOTED_OPEN).replace(STEER_MARKER_CLOSE, _QUOTED_CLOSE)


def summary_steer_piece(piece: str, redact) -> str:
    """Render one protected steer for summarizer input.

    The user's words are carried VERBATIM except for secret redaction: no
    clipping and no marker rewriting (a user may be asking about the exact
    delimiter text).
    """
    inner = steer_texts([piece])[0]
    return _STEER_PIECE_OPEN + redact(inner) + _STEER_BLOCK_CLOSE


class SummaryInput(str):
    """Serialized summarizer input carrying its protected steer positions.

    The serializer knows exactly where each rendered steer sits; budgeting
    reserves those ``(start, end)`` spans instead of searching the text for
    markers (a quoted closing marker inside a steer must not end its
    reservation, and lookalikes in ordinary text must get none). Any string
    operation yields a plain ``str`` (no spans), so positions can never
    drift from the text they describe.
    """

    protected_spans: Tuple[Tuple[int, int], ...] = ()

    def __new__(cls, text: str, spans=()):
        obj = super().__new__(cls, text)
        obj.protected_spans = tuple((int(a), int(b)) for a, b in spans)
        return obj


def summary_input_spans(text: Any) -> List[Tuple[int, int]]:
    """Protected spans a serializer attached to ``text`` (``[]`` if none)."""
    if not isinstance(text, SummaryInput):
        return []
    return list(text.protected_spans)
