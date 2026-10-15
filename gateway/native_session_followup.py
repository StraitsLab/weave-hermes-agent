"""Native request identity on the legacy queued follow-up lane."""
from __future__ import annotations
import asyncio
import inspect
import logging
from typing import Any, Optional
from gateway.platforms.event import MessageEvent
logger = logging.getLogger(__name__)

class NativeFollowupMixin:
    async def _begin_native_followup(self, pending_event: Any, session_key: str) -> Optional[MessageEvent]:
        """Weave (WEV-1726): announce a queued native submit as the active turn.

        Returns the event when the hook succeeded, so ``_end_native_followup``
        closes the same ref. Non-native follow-ups (and adapters without the
        hooks) return None: no behaviour change for them.

        A start failure is the FOLLOW-UP's failure, never the outer turn's: the
        outer request already produced and delivered its answer. So this never
        raises an ordinary exception to the caller. It closes whatever the hook
        may have activated (api_server installs the ref BEFORE its db await) as
        failed and returns None; the caller then skips the follow-up run.
        Cancellation is cleaned up the same way and then propagated.
        """
        meta = getattr(pending_event, "metadata", None) or {}
        if not (isinstance(meta, dict) and isinstance(meta.get("native_request_ref"), str) and meta["native_request_ref"]):
            return None
        adapter = self._adapter_for_source(getattr(pending_event, "source", None))
        started = getattr(adapter, "_on_native_submit_started", None)
        if not callable(started):
            return None
        try:
            result = started(pending_event, session_key)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            await self._end_native_followup(pending_event, session_key, failed=True)
            raise
        except Exception:  # health: allow BLE001 -- Best-effort event delivery must not fail the outer turn.
            logger.debug("Native follow-up start notification failed", exc_info=True)
            await self._end_native_followup(pending_event, session_key, failed=True)
            return None
        return pending_event

    @staticmethod
    def _native_followup_skipped(pending_event: Any) -> bool:
        """True when a native follow-up's start hook failed: it was closed as failed and must not run."""
        meta = getattr(pending_event, "metadata", None)
        return isinstance(meta, dict) and bool(meta.get("native_submit_failed"))

    async def _end_native_followup(self, pending_event: Optional[MessageEvent], session_key: str, *, failed: bool) -> None:
        if pending_event is None:
            return
        meta = getattr(pending_event, "metadata", None)
        if isinstance(meta, dict) and failed:
            meta["native_submit_failed"] = True
        adapter = self._adapter_for_source(getattr(pending_event, "source", None))
        finished = getattr(adapter, "_on_native_submit_finished", None)
        if not callable(finished):
            return
        try:
            result = finished(pending_event, session_key)
            if inspect.isawaitable(result):
                await result
        except Exception:  # health: allow BLE001 -- Best-effort event delivery must not fail the outer turn.
            logger.debug("Native follow-up finish notification failed", exc_info=True)

