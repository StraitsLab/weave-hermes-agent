"""Native cell configuration; turns use the gateway FIFO and existing prompt queues."""
import os
from contextvars import ContextVar

# Authenticated native admission identity, inherited by the gateway's executor.
native_turn_context = ContextVar("harso_native_turn_context", default=None)


def enabled():
    # Cells set gateway.native_bot_sessions in config.yaml (v0.21 read it there); the env switch also counts.
    if os.environ.get("HERMES_NATIVE_BOT_SESSIONS") == "1":  # health: allow HX002 -- Existing Harso installer switch; preserve deployed cell compatibility.
        return True
    from hermes_cli.config import load_config
    return (load_config().get("gateway") or {}).get("native_bot_sessions") is True


def capabilities():
    return {"native_session_inference": enabled(), "session_passive_append_group": True, "hub_revision_submit": True}


def pending(adapter, ref, kind):
    return None


async def respond(adapter, ref, prompt_id, value, kind):
    return None
