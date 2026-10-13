"""Project existing HTTP admission onto the stock persistent TUI session.

No agent loop, work queue or bot transport is implemented here. The native
prompt dispatcher and notification poller own execution and subsequent turns.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import nullcontext


def enabled() -> bool:
    from hermes_cli.config import load_config

    return (load_config().get("gateway") or {}).get("native_bot_sessions") is True


class Projection:
    trusted_controller = True
    requires_tool_lifecycle = True

    def __init__(self, adapter, profile, session_id, loop):
        self.adapter, self.profile, self.session_id, self.loop = (
            adapter,
            profile,
            session_id,
            loop,
        )
        self.ref = None
        self.responses = {}
        self.condition = threading.Condition()

    def close(self) -> None:
        pass

    def write(self, obj: dict) -> bool:
        frame = obj
        if "id" in frame:
            with self.condition:
                self.responses[frame["id"]] = frame
                self.condition.notify_all()
            return True
        event = frame.get("params") or {}
        if self.ref and event.get("type"):
            self.loop.call_soon_threadsafe(
                self._event, self.ref, event["type"], event.get("payload") or {}
            )
        return True

    def _event(self, ref, kind, payload):
        if not ref or ref != self.ref:
            return
        # Loop callbacks carry no request scope: enter this profile's home so
        # the session database (and secrets) resolve to the cell's profile.
        scope = getattr(self.adapter, "_profile_scope", None)
        with (scope(self.profile) if scope else nullcontext()):
            self._scoped_event(ref, kind, payload)

    def _scoped_event(self, ref, kind, payload):
        emit = self.adapter._native_submit_event
        if kind == "message.start":
            emit(ref, "turn.started")
        elif kind == "message.delta":
            emit(
                ref,
                "assistant.delta",
                delta=str(payload.get("text", payload.get("delta", "")))[:4096],
            )
        elif kind in ("tool.start", "tool.complete"):
            emit(
                ref,
                "tool.started" if kind == "tool.start" else "tool.completed",
                tool_call_id=str(payload.get("tool_id", payload.get("tool_call_id", payload.get("id", ""))))[
                    :256
                ],
                tool_name=str(payload.get("name", payload.get("tool", "")))[:256],
            )
        elif kind == "approval.request":
            emit(ref, kind, **self.adapter._native_approval_projection(payload))
        elif kind == "clarify.request":
            # Use the existing HTTP question projection and native waiter.
            questions = payload.get("questions") or [payload]
            if len(questions) != 1:
                self.rpc(
                    "clarify.respond",
                    {"request_id": payload["request_id"], "answer": ""},
                )
                return
            question = questions[0]
            item = {
                "type": kind,
                "native_request_ref": ref,
                "clarify_id": payload["request_id"],
                "question": question.get("question", ""),
                "choices": question.get("choices") or [],
                "multi_select": bool(question.get("multi_select", False)),
            }
            self.adapter._native_submit_events[ref] = [item]
            self.adapter._native_submit_clarifies[(ref, payload["request_id"])] = (
                "pending"
            )
            self.adapter.__dict__.setdefault("_native_bot_clarifies", {})[
                (ref, payload["request_id"])
            ] = (self, question.get("qid"))
            emit(
                ref,
                kind,
                **{
                    k: v
                    for k, v in item.items()
                    if k not in {"type", "native_request_ref"}
                },
            )
        elif kind == "message.complete":
            if payload.get("status") in {"error", "interrupted"}:
                self.adapter._native_submit_close(ref, "turn.failed")
            else:
                emit(
                    ref,
                    "assistant.final",
                    content=str(payload.get("text", ""))[:16384],
                    external_request_id=self.adapter._native_submit_external_ids.get(
                        ref, ""
                    ),
                )
                self.adapter._native_submit_close(ref, "turn.completed")
            for joined in self.adapter._native_submit_steered.pop(ref, []):
                self.adapter._native_submit_close(
                    joined,
                    "turn.failed"
                    if payload.get("status") in {"error", "interrupted"}
                    else "turn.completed",
                )
            self.ref = None

    def rpc(self, method, params):
        from tui_gateway import server
        import uuid

        rid = uuid.uuid4().hex
        result = server.dispatch(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}, self
        )
        if result is None:
            with self.condition:
                if not self.condition.wait_for(
                    lambda: rid in self.responses, timeout=30
                ):
                    raise RuntimeError("native RPC deadline")
                result = self.responses.pop(rid)
        if "error" in result:
            raise RuntimeError("native RPC unavailable")
        return result["result"]


async def _before_dispatch(function, *args):
    from gateway.native_awake import finish_owned
    operation = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError as cancelled:
        try:
            await finish_owned(operation)
        except Exception:
            pass  # No prompt was sent; preserve caller cancellation on worker failure.
        raise cancelled


async def _dispatch(adapter, transport, request_ref, method, params, admission):
    """Settle a sent RPC before surrendering ownership on HTTP cancellation."""
    from gateway.native_awake import finish_owned

    adapter._native_submit_handoffs[request_ref] = "dispatch_pending"
    operation = asyncio.create_task(asyncio.to_thread(transport.rpc, method, params))
    try:
        result = await asyncio.shield(operation)
    except asyncio.CancelledError:
        try:
            result = await finish_owned(operation)
            if result.get("status") == ("queued" if admission == "steered" else "streaming"):
                adapter._reconcile_native_submit_evidence(request_ref, admission)
                adapter._native_submit_handoffs[request_ref] = admission
            else:
                adapter._native_submit_handoffs.pop(request_ref, None)
        except Exception:
            # The sent RPC failed without a definitive refusal: genuinely ambiguous.
            pass
        raise
    return result


APPROVE_WORDS = frozenset({"approve", "yes", "ok", "okay", "confirm", "y", "yes please", "go ahead", "do it", "sure"})
DENY_WORDS = frozenset({"deny", "no", "reject", "cancel", "n", "no thanks", "stop", "don't"})


def _answer_blocking_approval(session_key, message):
    """True when `message` was a bare answer to the session's oldest blocking approval and resolved it."""
    from tools.approval import has_blocking_approval, resolve_gateway_approval

    if not has_blocking_approval(session_key):
        return False
    word = " ".join((message or "").strip().lower().rstrip(".!").split())
    if word in APPROVE_WORDS:
        return bool(resolve_gateway_approval(session_key, "once"))
    if word in DENY_WORDS:
        return bool(resolve_gateway_approval(session_key, "deny"))
    return False


async def admit(
    adapter,
    profile,
    session_id,
    message,
    request_ref,
    external_id,
    *,
    internal=False,
    busy_mode="queue",
    expected_active_ref=None,
):
    from tui_gateway import server

    owner = (profile, session_id)
    transports = adapter.__dict__.setdefault("_native_bot_transports", {})
    transport = transports.get(owner)
    if transport is None:
        transport = Projection(adapter, profile, session_id, asyncio.get_running_loop())
        transports[owner] = transport
    resumed = await _before_dispatch(
        transport.rpc,
        "session.resume",
        {"session_id": session_id, "profile": profile, "omit_messages": True},
    )
    sid = resumed["session_id"]
    session, error = await _before_dispatch(server._sess, {"session_id": sid}, "admit")
    if error or session is None or session.get("agent") is None:
        raise RuntimeError("native session is not ready")
    # The HTTP request ending does not end the bot: this transport is the cell's
    # own controller, and native completion notifications resume the same agent.
    if internal:
        busy_mode = "queue"
    key = session["session_key"]
    # DEC-415: a bare yes/no while the agent waits on an approval answers it (as the Slack path does
    # in gateway/run.py). A spoken "yes" on a call reaches the cell as a plain send; nothing else does.
    if not internal and _answer_blocking_approval(key, message):
        adapter._native_submit_handoffs[request_ref] = "answered_approval"
        return "answered_approval"
    active_ref = adapter._native_submit_active_ref(key)
    if (
        expected_active_ref is not None
        and busy_mode != "queue"
        and active_ref != expected_active_ref
    ):
        return "target_changed"
    if session.get("running"):
        if busy_mode == "steer" and active_ref:
            # Link before sending so terminal evidence can close an ambiguous steer.
            adapter._native_submit_ref_sessions[request_ref] = (profile, session_id)
            adapter._native_submit_steered.setdefault(active_ref, []).append(request_ref)
            result = await _dispatch(adapter, transport, request_ref,
                "session.steer", {"session_id": sid, "text": message}, "steered")
            if result.get("status") == "queued":
                adapter._native_submit_ref_sessions[request_ref] = (profile, session_id)
                adapter._native_submit_handoffs[request_ref] = "steered"
                return "steered"
            adapter._native_submit_handoffs.pop(request_ref, None)
            adapter._native_submit_steered.get(active_ref, []).remove(request_ref)
        if busy_mode == "interrupt":
            await asyncio.to_thread(
                transport.rpc, "session.interrupt", {"session_id": sid}
            )
        # Retry durable admission after native cancellation settles. Never
        # replace the native single queued slot or acknowledge unowned input.
        raise RuntimeError("native bot is busy")
    transport.ref = request_ref
    session["agent"]._relay_pending_turn_id = external_id
    key = session["session_key"]
    adapter._native_submit_active_refs[key] = request_ref
    adapter._native_submit_approval_keys[request_ref] = key
    adapter._native_submit_external_ids[request_ref] = external_id
    adapter._native_submit_ref_sessions[request_ref] = (profile, session_id)
    # Dispatch timeout is ambiguous: retain the hold until terminal evidence
    # or bounded expiry, never release potentially live work on HTTP failure.
    result = await _dispatch(
        adapter, transport, request_ref, "prompt.submit",
        {
            "session_id": sid,
            "text": message,
            "queued": True,
            **({"display_kind": "hidden"} if internal else {}),
        }, "streaming",
    )
    if result.get("status") != "streaming":
        transport.ref = None
        adapter._native_submit_handoffs.pop(request_ref, None)
        raise RuntimeError("native prompt was not admitted")
    adapter._native_submit_handoffs[request_ref] = "streaming"
    return "streaming"
