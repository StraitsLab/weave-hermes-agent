"""Weave (WEV-3349): copy what the user sees in a chat into Supabase ``public.chat_items``.

One daemon thread per gateway process. Two feeds:

* live: ``APIServerAdapter._native_submit_event`` hands every live event here (``publish``).
  Text deltas of a turn grow ONE ``writing`` row; approvals and clarifications become
  ``pending`` card rows. Pieces are flushed at most every ``FLUSH_S``. A ``tool.completed``
  that carries an answer card (WEV-3395) puts the turn's last card on its answer row as
  ``body.output``, the field the apps already draw.
* saved: when a turn ends, the session's display rows (the set ``/messages`` serves,
  compacted turns included) are upserted ``final``. The turn's assistant answer adopts
  the live row, so the streamed text and the saved message are the same row.

Keys survive compaction (it copies rows with the same timestamp but a new SQLite id):
every row is keyed ``<role>:<timestamp>``. A user message the app sent through Supabase is
already a row there; the database matches the cell's copy to it by text and stamps the key.

Credentials are read in the turn's own profile scope: WEAVE_CHAT_SUPABASE_URL and
WEAVE_CHAT_SUPABASE_KEY (publishable key) switch it on; the cell proves itself to the
database with the route bearer it already holds (WEAVE_API_MCP_BEARER).

Visible replies (DEC-415): on a live call the voice speaks the assistant's reply and the
screen shows it only when the model marks it. A reply that starts with the show tag
(WEAVE_CHAT_SHOW_TAG, default ``[SHOW]``) loses the tag and its saved row carries
``body.visible = true``; so does a reply with a link or attachment, or one longer than
WEAVE_CHAT_SHOW_OVER_CHARS (default 400). Every other assistant row carries ``visible = false``
and the apps decide by context: a chat screen shows all rows, a call screen only visible ones.
"""
from __future__ import annotations

import collections
import json
import logging
import queue
import re
import threading
import time
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

FLUSH_S = 0.2  # live text cadence: Supabase bills each Broadcast, 5/s per reply
FINAL_RETRY_S = 600  # bound retained turns during a sustained Supabase outage
FINAL_BACKOFF_S = 30
CARD_TYPES = {"approval.request": "approval", "clarify.request": "clarify"}
SCAFFOLDING = ("[System:", "[IMPORTANT: Background", "[Your active task list", "[OUT-OF-BAND",
               "[CONTEXT COMPACTION", "[ASYNC DELEGATION", "[Planning state preserved")


HUB = "mcp__weave_connectors__"  # the cell's name prefix for Harso hub tools
CODE_CARDS = {HUB + "weather_now", HUB + "page_read"}  # hub tools whose own code builds a card


def hub_card(tool_name: Any, result: Any) -> Optional[dict[str, Any]]:
    """WEV-3395: the answer card a Harso card tool returned (MCP structuredContent or result ``card``), else None.

    Only Harso's card tools count: ``show_<card>`` (weave-api fills and checks it) and CODE_CARDS. Every other hub
    tool can carry third-party output (Exa results, forwarded ``<provider>__<tool>`` connectors), so it never does.
    ``result`` is the raw MCP JSON string (a guardrail note may follow it) or that JSON already parsed.
    """
    name = str(tool_name or "")
    if name not in CODE_CARDS and not (name.startswith(HUB + "show_") and "__" not in name[len(HUB):]):
        return None
    try:
        payload = json.JSONDecoder().raw_decode(result)[0] if isinstance(result, str) else result
        content = payload.get("structuredContent") or payload.get("result")
        if isinstance(content, str):  # upstream deduplicates structuredContent against JSON TextContent.
            content = json.JSONDecoder().raw_decode(content)[0]
        card = content.get("card")
    except (ValueError, AttributeError):
        return None
    return card if isinstance(card, dict) and card.get("kind") == "output_blocks" else None


def row_key(message: dict[str, Any]) -> str:
    return f"{message['role']}:{message.get('timestamp')!r}"


_LINKISH = ("http://", "https://", "](", "<artifact", "file://")


def visible_reply(text: str, show_tag: str = "[SHOW]", show_over: int = 400) -> tuple[str, bool]:
    """(text without the show tag, whether the screen shows it on a call). DEC-415."""
    stripped = text.lstrip()
    if show_tag and stripped.startswith(show_tag):
        return stripped[len(show_tag):].lstrip(), True
    return text, any(mark in text for mark in _LINKISH) or len(text) > show_over


def visible_rows(messages: list[dict[str, Any]], show_tag: str = "[SHOW]", show_over: int = 400) -> list[dict[str, Any]]:
    """The rows a person sees: what they said and what the assistant answered (no tool channel)."""
    out = []
    for m in messages:
        content = m.get("content")
        if m.get("role") not in ("user", "assistant") or m.get("display_kind") in ("hidden", "internal_notification"):
            continue
        if not isinstance(content, str) or not content.strip():
            continue  # tool-call carriers
        if m["role"] == "user" and content.lstrip().startswith(SCAFFOLDING):
            continue
        at = m.get("timestamp")
        body: dict[str, Any] = {"text": content}
        if m["role"] == "assistant":
            body["text"], body["visible"] = visible_reply(content, show_tag, show_over)
        out.append({"cell_key": row_key(m), "role": m["role"], "kind": "text", "state": "final",
                    "body": body,
                    **({"at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(at)) + f".{int(at % 1 * 1e6):06d}Z"}
                       if isinstance(at, (int, float)) else {})})
    return out


class _Writer:
    def __init__(self) -> None:
        self.events: queue.Queue[tuple] = queue.Queue(maxsize=10_000)
        self.live: dict[str, dict[str, Any]] = {}
        threading.Thread(target=self._run, name="weave-chat-writer", daemon=True).start()

    def _run(self) -> None:
        while True:
            deadline = time.monotonic() + FLUSH_S
            while True:
                try:
                    ref, event = self.events.get(timeout=max(0.0, deadline - time.monotonic()))
                except queue.Empty:
                    break
                self._take(ref, event)
            for ref, live in list(self.live.items()):
                self._write(ref, live)

    def _write(self, ref: str, live: dict[str, Any]) -> None:
        ended = live.get("ended", False)
        now = time.monotonic()
        if ended and now >= live.get("retry_until", float("inf")):
            logger.error("chat writer: final write retries exhausted for %s", ref)
            self.live.pop(ref, None)
            _ENDED.append(ref)
            return
        if now < live.get("retry_at", 0):
            return
        try:
            if ended and "messages" not in live:
                # Completion can admit another turn while we retry. Never adopt that turn's answer.
                live["messages"] = live["read"]()
            self._flush(live)
            if ended:
                self._finish(live)
                self.live.pop(ref, None)
                _ENDED.append(ref)
        except Exception:
            logger.warning("chat writer: write failed", exc_info=True)
            if ended:
                now = time.monotonic()
                live.setdefault("retry_until", now + FINAL_RETRY_S)
                delay = live.get("retry_delay", 1)
                live["retry_at"] = now + delay
                live["retry_delay"] = min(delay * 2, FINAL_BACKOFF_S)
        finally:
            # Completion releases the native awake lease. Retrying Supabase must not hold it.
            if ended:
                callback = live.pop("on_finished", None)
                if callback:
                    callback()

    def _take(self, ref: str, event: dict[str, Any]) -> bool:
        live, kind = self.live.get(ref), event.get("type")
        if live is None or live.get("ended"):
            return False
        if kind == "assistant.delta" and isinstance(event.get("delta"), str):
            live["text"] += event["delta"]
        elif kind in ("tool.started", "tool.generating"):  # generating: the model began writing the call (WEV-3464)
            live["status"] = str(event.get("tool_name") or "")[:120]
        elif kind in ("tool.completed", "tool.failed"):
            live["status"] = None
            if isinstance(event.get("card"), dict):  # WEV-3395: the turn's last card goes on its answer row
                live["card"] = event["card"]
                live["card_sent"] = False  # WEV-3464: and onto the live row at once
        elif kind in CARD_TYPES:
            card_id = event.get("request_id") or event.get("clarify_id") or ""
            body = {k: v for k, v in event.items()
                    if k not in ("type", "sequence", "native_request_ref", "command", "pattern_key")}
            body["native_request_ref"] = ref  # WEV-3404: an answer row reaches the cell by the turn's ref
            if kind == "approval.request":
                pattern = str(event.get("pattern_key") or "")
                consent = pattern.removeprefix("plugin_rule:") if pattern.startswith("plugin_rule:") else None
                # A Ledger card owns Always; native Hermes still receives only Once when it resumes.
                ledger_card = consent and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", consent)
                body |= {"approval_ref": consent, "choices": ["once", "always", "deny"] if ledger_card else ["once", "deny"]}
            card = {"cell_key": f"{CARD_TYPES[kind]}:{card_id}", "role": "assistant",
                    "kind": CARD_TYPES[kind], "state": "pending", "body": body}
            live["cards"].append(card)
            live["card_keys"].append(card["cell_key"])
        if kind in ("turn.completed", "turn.failed"):
            live["ended"] = True
            live["on_finished"] = event.get("_on_finished")
            live["outcome"] = kind.split(".")[1]
            return True
        return False

    def _flush(self, live: dict[str, Any]) -> None:
        cards = live["cards"]
        if cards:
            _call(live, cards)
            live["cards"] = []  # keep their cell keys queued until the upsert is acknowledged
        piece = live["text"][live["sent"]:]
        if live["rev"] is not None and live.get("card_sent") is False:
            # A body-only write: the push trigger sends the whole body (card included) only when nothing is appended.
            out = _call(live, [{"item_id": live["item"], "role": "assistant", "kind": "text", "state": "writing",
                                "body": {"output": live["card"], "status": live["status"]}}])[0]
            live["rev"], live["shown"], live["card_sent"] = out["rev"], live["status"], True
        if live["rev"] is not None and not piece and live["status"] == live["shown"]:
            return
        # One call carries the new text and the status line ("Searching the web"); the first one opens the row.
        item = {"item_id": live["item"], "role": "assistant", "kind": "text", "state": "writing",
                "append": piece, "body": {"status": live["status"]}}
        if live["rev"] is not None:
            item["rev"] = live["rev"]
        out = _call(live, [item])[0]
        if out.get("conflict"):  # the row moved under us: send the whole text once
            out = _call(live, [{"item_id": live["item"], "role": "assistant", "kind": "text", "state": "writing",
                                "body": {"text": live["text"], "status": live["status"]}}])[0]
        live["sent"], live["rev"], live["shown"] = len(live["text"]), out["rev"], live["status"]

    def _finish(self, live: dict[str, Any]) -> None:
        messages = live["messages"] if "messages" in live else live["read"]()
        # Only this turn's rows. Hermes saves the turn's own user message with its request ref as
        # platform_message_id; everything from there on is this turn. Earlier rows are already in Supabase.
        start = next((i for i, m in enumerate(messages) if m.get("platform_message_id") == live["ref"]), None)
        turn = messages[start:] if start is not None else [m for m in messages if (m.get("timestamp") or 0) >= live["started"]]
        rows = visible_rows(turn, live["show_tag"], live["show_over"])
        # This turn's answer: its last visible assistant message, saved after the turn started.
        mine = [m for m in messages if m.get("role") == "assistant" and isinstance(m.get("content"), str)
                and m["content"].strip() and (m.get("timestamp") or 0) >= live["started"]]
        answer = next((r for r in rows if mine and r["cell_key"] == row_key(mine[-1])), None)
        if answer is not None and live.get("outcome") == "completed":
            rows.remove(answer)
            answer["item_id"] = live["item"]  # the streamed row becomes the saved answer
            answer["body"]["status"] = None
            if live.get("card"):
                answer["body"]["output"] = live["card"]
            last = answer
        else:  # no answer: hide any partial reply. Either way the live row tells the apps the turn ended.
            last = {"item_id": live["item"], "role": "assistant", "kind": "text", "state": "removed",
                    "body": {"outcome": live.get("outcome") or "failed", "status": None}}
        rows += [{"cell_key": key, "role": "assistant", "state": "resolved"} for key in live["card_keys"]]
        rows.append(last)  # last, so every saved row is in Supabase before the apps settle the turn
        for first in range(0, len(rows), 100):
            _call(live, rows[first:first + 100])


def _call(live: dict[str, Any], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    body = json.dumps({"p_bearer": live["bearer"], "p_session_ref": live["session"], "p_items": items}).encode()
    request = urllib.request.Request(live["url"] + "/rest/v1/rpc/chat_cell_write", data=body, method="POST",
                                     headers={"apikey": live["key"], "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


_WRITER: Optional[_Writer] = None
_LOCK = threading.Lock()
_ENDED: collections.deque[str] = collections.deque(maxlen=256)  # refs already finished: a late event reopens nothing


def publish(session_id: str, ref: str, event: dict[str, Any], read_display: Callable[[], list[dict[str, Any]]],
            secret: Callable[[str], str], *, on_finished=None) -> bool:
    """Hand one live event to the writer. Never blocks and never raises into the gateway."""
    global _WRITER
    try:
        if _WRITER is None or ref not in _WRITER.live:
            # A turn opens on its first event: an immediate turn emits no turn.started (only queued or steered
            # ones do). Even a bare terminal event opens it, so the turn's rows are saved and its live row ends.
            if ref in _ENDED:
                return
            url = (secret("WEAVE_CHAT_SUPABASE_URL") or "").rstrip("/")
            key, bearer = secret("WEAVE_CHAT_SUPABASE_KEY") or "", secret("WEAVE_API_MCP_BEARER") or ""
            if not (url.startswith("https://") and key and bearer and session_id.startswith("weave-")):
                return
            show_tag = (secret("WEAVE_CHAT_SHOW_TAG") or "[SHOW]").strip()
            try:
                show_over = int(secret("WEAVE_CHAT_SHOW_OVER_CHARS") or 400)
            except ValueError:
                show_over = 400
            with _LOCK:
                _WRITER = _WRITER or _Writer()
            _WRITER.live[ref] = {"session": session_id, "ref": ref, "url": url, "key": key, "bearer": bearer, "started": time.time() - 1,
                                 "read": read_display, "item": str(uuid.uuid5(uuid.NAMESPACE_URL, f"weave-chat:{ref}")),
                                 "text": "", "sent": 0, "rev": None, "status": None, "shown": None,
                                 "cards": [], "card_keys": [], "show_tag": show_tag, "show_over": show_over}
            if event.get("type") == "turn.started":
                return
        _WRITER.events.put_nowait((ref, {**event, "_on_finished": on_finished} if on_finished else event))
        return True
    except queue.Full:
        logger.warning("chat writer queue full; live piece dropped (the saved row still lands)")
    except Exception:
        logger.debug("chat writer publish skipped", exc_info=True)


def publish_rows(session_id: str, messages: list[dict[str, Any]], secret: Callable[[str], str]) -> bool:
    """Copy finished rows that arrived outside a turn (a routine brief) into ``chat_items``. Never raises."""
    try:
        url = (secret("WEAVE_CHAT_SUPABASE_URL") or "").rstrip("/")
        key, bearer = secret("WEAVE_CHAT_SUPABASE_KEY") or "", secret("WEAVE_API_MCP_BEARER") or ""
        if not (url.startswith("https://") and key and bearer and session_id.startswith("weave-")):
            return False
        rows = visible_rows(messages, (secret("WEAVE_CHAT_SHOW_TAG") or "[SHOW]").strip())
        if rows:
            _call({"url": url, "key": key, "bearer": bearer, "session": session_id}, rows)
        return True
    except Exception:
        logger.warning("chat rows copy failed", exc_info=True)
        return False
