"""Project existing HTTP admission onto the stock persistent TUI session.

No agent loop, work queue or bot transport is implemented here. The native
prompt dispatcher and notification poller own execution and subsequent turns.
"""

from __future__ import annotations

import asyncio
import threading


def enabled() -> bool:
    from hermes_cli.config import load_config

    return (load_config().get("gateway") or {}).get("native_bot_sessions") is True


class Projection:
    trusted_controller = True

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
                tool_call_id=str(payload.get("tool_call_id", payload.get("id", "")))[
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
    resumed = await asyncio.to_thread(
        transport.rpc,
        "session.resume",
        {"session_id": session_id, "profile": profile, "omit_messages": True},
    )
    sid = resumed["session_id"]
    session, error = await asyncio.to_thread(server._sess, {"session_id": sid}, "admit")
    if error or session is None or session.get("agent") is None:
        raise RuntimeError("native session is not ready")
    # The HTTP request ending does not end the bot: this transport is the cell's
    # own controller, and native completion notifications resume the same agent.
    if internal:
        busy_mode = "queue"
    key = session["session_key"]
    active_ref = adapter._native_submit_active_ref(key)
    if (
        expected_active_ref is not None
        and busy_mode != "queue"
        and active_ref != expected_active_ref
    ):
        return "target_changed"
    if session.get("running"):
        if busy_mode == "steer" and active_ref:
            result = await asyncio.to_thread(
                transport.rpc, "session.steer", {"session_id": sid, "text": message}
            )
            if result.get("status") == "queued":
                adapter._native_submit_ref_sessions[request_ref] = (profile, session_id)
                adapter._native_submit_steered.setdefault(active_ref, []).append(
                    request_ref
                )
                adapter._native_submit_handoffs[request_ref] = "steered"
                return "steered"
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
    result = await asyncio.to_thread(
        transport.rpc,
        "prompt.submit",
        {
            "session_id": sid,
            "text": message,
            "queued": True,
            **({"display_kind": "hidden"} if internal else {}),
        },
    )
    if result.get("status") != "streaming":
        transport.ref = None
        raise RuntimeError("native prompt was not admitted")
    adapter._native_submit_handoffs[request_ref] = "streaming"
    return "streaming"
