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

Steer identification for pruning/compaction uses the existing steer marker
text (``STEER_MARKER_OPEN`` … ``STEER_MARKER_CLOSE``) peeled from the END of
the tool content only — no schema or row metadata, and it survives reload
because it IS the persisted content. See ``split_trailing_appends``.
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
                )
                if updated != 1:
                    _log_refusal(agent, "stored row changed", row_id)
                    return False
        msg["content"] = new_content
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
# Pruning / compaction: keep steer text through tool-content rewrites
# ---------------------------------------------------------------------------


def _is_steer_block_text(text: str) -> bool:
    t = text.lstrip()
    return t.startswith(_STEER_BLOCK_OPEN) and t.endswith(_STEER_BLOCK_CLOSE)


def split_trailing_appends(content: Any) -> Tuple[Any, List[str]]:
    """Peel appended steer / wrap-up parts off the END of tool content.

    Returns ``(body, steers)`` where ``body`` is the tool output with every
    trailing steer marker block and wrap-up notice removed, and ``steers`` is
    the list of steer markers in original order, each in string-append form
    (``"\\n\\n" + OPEN + "\\n" + text + "\\n" + CLOSE``) so callers can re-attach
    them verbatim after a summary. The wrap-up notice is dropped (it may be
    pruned). Only the tail is examined: a lookalike marker in the middle of a
    tool output is ordinary output.
    """
    steers: List[str] = []
    if isinstance(content, str):
        body = content
        while True:
            if body.endswith("\n\n" + RUN_BUDGET_WRAPUP_NOTICE):
                body = body[: -len("\n\n" + RUN_BUDGET_WRAPUP_NOTICE)]
                continue
            if body.endswith(_STEER_BLOCK_CLOSE):
                start = body.rfind("\n\n" + _STEER_BLOCK_OPEN)
                if start >= 0:
                    steers.insert(0, body[start:])
                    body = body[:start]
                    continue
            break
        return body, steers
    if isinstance(content, list):
        blocks = list(content)
        while blocks:
            last = blocks[-1]
            text = last.get("text") if isinstance(last, dict) and last.get("type") == "text" else None
            if not isinstance(text, str):
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


def steer_texts(steers: List[str]) -> List[str]:
    """The user's words inside each steer marker (for summaries)."""
    out = []
    for s in steers:
        t = s.strip()
        if t.startswith(_STEER_BLOCK_OPEN) and t.endswith(_STEER_BLOCK_CLOSE):
            t = t[len(_STEER_BLOCK_OPEN): -len(_STEER_BLOCK_CLOSE)]
        out.append(t)
    return out


def with_steers(new_text: str, steers: Optional[List[str]]) -> str:
    """Re-attach preserved steer markers after a rewritten tool body."""
    return new_text + "".join(steers or [])
