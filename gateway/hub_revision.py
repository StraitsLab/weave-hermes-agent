"""Desired schema revision is metadata, never synchronous chat maintenance."""
import asyncio
import logging
import re
import uuid
from gateway import native_awake


def validate(body):
    return (isinstance(body, dict) and set(body) == {"server", "revision"}
            and body["server"] == "weave-connectors" and isinstance(body["revision"], str)
            and re.fullmatch(r"[a-f0-9]{64}", body["revision"]) is not None)


class HubRevision:
    def __init__(self, adapter):
        self.adapter = adapter
        self.desired = self.applied = None
        self.task = None

    def request(self, revision):
        self.desired = revision
        if self.applied == revision or (self.task and not self.task.done()):
            return
        self.task = asyncio.create_task(self._refresh())

    async def _refresh(self):
        lease = uuid.uuid4().hex
        held = False
        try:
            await native_awake.acquire(lease)
            held = True
            while self.desired != self.applied:
                revision = self.desired
                # Use the adapter's existing full/scoped exclusion; no second
                # concurrent registry writer, and only wait in background.
                # The adapter only gains this flag once the HTTP reload route has run.
                while getattr(self.adapter, "_mcp_reload_in_progress", False):
                    await asyncio.sleep(.1)
                self.adapter._mcp_reload_in_progress = True
                try:
                    result = await self.adapter.gateway_runner._reload_mcp_toolset(server="weave-connectors")
                    if result.get("schema_refreshed") != "weave-connectors":
                        raise RuntimeError("missing scoped refresh acknowledgment")
                    self.applied = revision
                finally:
                    self.adapter._mcp_reload_in_progress = False
        except Exception as error:  # health: allow BLE001,S110 -- HTTP/credential boundary: log the type, never the contents.
            logging.getLogger(__name__).warning("hub revision pending after refresh failure (%s)", type(error).__name__)
        finally:
            if held:
                native_awake.release(lease)
