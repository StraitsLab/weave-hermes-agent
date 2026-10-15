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
import threading
import time
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

FLUSH_S = 0.2  # live text cadence: Supabase bills each Broadcast, 5/s per reply
CARD_TYPES = {"approval.request": "approval", "clarify.request": "clarify"}
SCAFFOLDING = ("[System:", "[IMPORTANT: Background", "[Your active task list", "[OUT-OF-BAND",
               "[CONTEXT COMPACTION", "[ASYNC DELEGATION", "[Planning state preserved")


HUB = "mcp__weave_connectors__"  # the cell's name prefix for Harso hub tools
CODE_CARDS = {HUB + "weather_now", HUB + "page_read"}  # hub tools whose own code builds a card


def hub_card(tool_name: Any, result: Any) -> Optional[Dict[str, Any]]:
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
        card = (payload.get("structuredContent") or payload.get("result")).get("card")
    except (ValueError, AttributeError):
        return None
    return card if isinstance(card, dict) and card.get("kind") == "output_blocks" else None


def row_key(message: Dict[str, Any]) -> str:
    return f"{message['role']}:{message.get('timestamp')!r}"


_LINKISH = ("http://", "https://", "](", "<artifact", "file://")


def visible_reply(text: str, show_tag: str = "[SHOW]", show_over: int = 400) -> "tuple[str, bool]":
    """(text without the show tag, whether the screen shows it on a call). DEC-415."""
    stripped = text.lstrip()
    if show_tag and stripped.startswith(show_tag):
        return stripped[len(show_tag):].lstrip(), True
    return text, any(mark in text for mark in _LINKISH) or len(text) > show_over


def visible_rows(messages: List[Dict[str, Any]], show_tag: str = "[SHOW]", show_over: int = 400) -> List[Dict[str, Any]]:
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
        body: Dict[str, Any] = {"text": content}
        if m["role"] == "assistant":
            body["text"], body["visible"] = visible_reply(content, show_tag, show_over)
        out.append({"cell_key": row_key(m), "role": m["role"], "kind": "text", "state": "final",
                    "body": body,
                    **({"at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(at)) + f".{int(at % 1 * 1e6):06d}Z"}
                       if isinstance(at, (int, float)) else {})})
    return out


class _Writer:
    def __init__(self) -> None:
        self.events: "queue.Queue[tuple]" = queue.Queue(maxsize=10_000)
        self.live: Dict[str, Dict[str, Any]] = {}
        threading.Thread(target=self._run, name="weave-chat-writer", daemon=True).start()

    def _run(self) -> None:
        while True:
            deadline, ended = time.monotonic() + FLUSH_S, []
            while True:
                try:
                    ref, event = self.events.get(timeout=max(0.0, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if self._take(ref, event):
                    ended.append(ref)
            for ref, live in list(self.live.items()):
                try:
                    self._flush(live)
                    if ref in ended:
                        self.live.pop(ref, None)
                        _ENDED.append(ref)
                        self._finish(live)
                except Exception:  # noqa: BLE001 - Supabase down: keep going; the next turn end resyncs
                    logger.warning("chat writer: write failed", exc_info=True)
                    if ref in ended:
                        self.live.pop(ref, None)
                        _ENDED.append(ref)
                finally:
                    if ref in ended and live.get("on_finished"):
                        live["on_finished"]()

    def _take(self, ref: str, event: Dict[str, Any]) -> bool:
        live, kind = self.live.get(ref), event.get("type")
        if live is None:
            return False
        if kind == "assistant.delta" and isinstance(event.get("delta"), str):
            live["text"] += event["delta"]
        elif kind == "tool.started":
            live["status"] = str(event.get("tool_name") or "")[:120]
        elif kind in ("tool.completed", "tool.failed"):
            live["status"] = None
            if isinstance(event.get("card"), dict):  # WEV-3395: the turn's last card goes on its answer row
                live["card"] = event["card"]
        elif kind in CARD_TYPES:
            card_id = event.get("request_id") or event.get("clarify_id") or ""
            body = {k: v for k, v in event.items()
                    if k not in ("type", "sequence", "native_request_ref", "command", "pattern_key")}
            body["native_request_ref"] = ref  # WEV-3404: an answer row reaches the cell by the turn's ref
            if kind == "approval.request":
                pattern = str(event.get("pattern_key") or "")
                consent = pattern.removeprefix("plugin_rule:") if pattern.startswith("plugin_rule:") else None
                # shortcut: a consent card offers once/deny here (its Always choice lives in the Ledger); the API
                # answer route still decides. Upgrade when the card row carries the Ledger's choices.
                body |= {"approval_ref": consent, "choices": ["once", "deny"]}
            card = {"cell_key": f"{CARD_TYPES[kind]}:{card_id}", "role": "assistant",
                    "kind": CARD_TYPES[kind], "state": "pending", "body": body}
            live["cards"].append(card)
            live["card_keys"].append(card["cell_key"])
        if kind in ("turn.completed", "turn.failed"):
            live["on_finished"] = event.get("_on_finished")
            live["outcome"] = kind.split(".")[1]
            return True
        return False

    def _flush(self, live: Dict[str, Any]) -> None:
        cards, live["cards"] = live["cards"], []
        if cards:
            _call(live, cards)
        piece = live["text"][live["sent"]:]
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

    def _finish(self, live: Dict[str, Any]) -> None:
        messages = live["read"]()
        # Only this turn's rows: what arrived since it started, with slack for the steered sends it absorbed.
        # Earlier rows are already in Supabase (every chat has synced since 2026-10-13); re-sending the whole
        # transcript after a gateway restart re-stamped 71 rows and flooded the app (2026-10-15).
        rows = visible_rows([m for m in messages if (m.get("timestamp") or 0) >= live["started"] - 120],
                            live["show_tag"], live["show_over"])
        # This turn's answer: its last visible assistant message, saved after the turn started.
        mine = [m for m in messages if m.get("role") == "assistant" and isinstance(m.get("content"), str)
                and m["content"].strip() and (m.get("timestamp") or 0) >= live["started"]]
        answer = next((r for r in rows if mine and r["cell_key"] == row_key(mine[-1])), None)
        if answer is not None and live.get("outcome") == "completed":
            rows.remove(answer)
            answer["item_id"] = live["item"]  # the streamed row becomes the saved answer
            answer["body"]["status"] = None
            answer.pop("at", None)  # keep the row's open time: it sorts under the send it answers, not under a later steer
            if live.get("card"):
                answer["body"]["output"] = live["card"]
            last = answer
        else:  # no answer: hide any partial reply. Either way the live row tells the apps the turn ended.
            last = {"item_id": live["item"], "role": "assistant", "kind": "text", "state": "removed",
                    "body": {"outcome": live.get("outcome") or "failed", "status": None}}
        rows += [{"cell_key": key, "role": "assistant", "state": "resolved"} for key in live["card_keys"]]
        rows.append(last)  # last, so every saved row is in Supabase before the apps settle the turn
        for start in range(0, len(rows), 100):
            _call(live, rows[start:start + 100])


def _call(live: Dict[str, Any], items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    body = json.dumps({"p_bearer": live["bearer"], "p_session_ref": live["session"], "p_items": items}).encode()
    request = urllib.request.Request(live["url"] + "/rest/v1/rpc/chat_cell_write", data=body, method="POST",
                                     headers={"apikey": live["key"], "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


_WRITER: Optional[_Writer] = None
_LOCK = threading.Lock()
_ENDED: "collections.deque[str]" = collections.deque(maxlen=256)  # refs already finished: a late event reopens nothing


def publish(session_id: str, ref: str, event: Dict[str, Any], read_display: Callable[[], List[Dict[str, Any]]],
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
            _WRITER.live[ref] = {"session": session_id, "url": url, "key": key, "bearer": bearer, "started": time.time() - 1,
                                 "read": read_display, "item": str(uuid.uuid5(uuid.NAMESPACE_URL, f"weave-chat:{ref}")),
                                 "text": "", "sent": 0, "rev": None, "status": None, "shown": None,
                                 "cards": [], "card_keys": [], "show_tag": show_tag, "show_over": show_over}
            if event.get("type") == "turn.started":
                return
        _WRITER.events.put_nowait((ref, {**event, "_on_finished": on_finished} if on_finished else event))
        return True
    except queue.Full:
        logger.warning("chat writer queue full; live piece dropped (the saved row still lands)")
    except Exception:  # noqa: BLE001
        logger.debug("chat writer publish skipped", exc_info=True)
