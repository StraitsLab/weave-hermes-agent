"""Harso Web Bot Auth interception; upstream owns dialogs and frame tracking."""
from __future__ import annotations
import asyncio
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit
from tools.browser_supervisor_dialogs import DIALOG_BRIDGE_HOST, DIALOG_BRIDGE_URL_PATTERN, DialogSupervisionMixin

logger = logging.getLogger("tools.browser_supervisor")

def _is_dialog_bridge(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return (parts.scheme, parts.netloc) == ("http", DIALOG_BRIDGE_HOST)
    except ValueError:
        return False

class RequestSigningMixin:
    async def _install_dialog_bridge(self, session_id):
        self._signing_sessions.add(session_id)
        await DialogSupervisionMixin._install_dialog_bridge(self, session_id)

    async def _on_target_detached(self, params, session_id=None):
        self._signing_sessions.discard(params.get("sessionId"))
        from tools.browser_supervisor_frames import FrameTrackingMixin
        result = FrameTrackingMixin._on_target_detached(self, params, session_id)
        if result is not None:
            await result

    async def _enable_bridge_fetch(self, session_id, strict=False):
        try:
            await self._cdp("Fetch.enable", {"patterns": [{"urlPattern": DIALOG_BRIDGE_URL_PATTERN,
                "requestStage": "Request"}], "handleAuthRequests": False}, session_id=session_id, timeout=5.0)
        except Exception:
            if strict:
                raise

    def set_request_signer(self, signer: Any, timeout: float = 5.0) -> None:
        """Web Bot Auth (fork, AB-2): sign with ``signer`` from now on, or stop intercepting when it is None.

        The registry calls it on every lookup, so ``browser.web_bot_auth`` governs existing sessions in both
        directions. Only the browser-level interceptor changes; the page sessions' dialog bridge is untouched.
        A failed enable/disable or session re-enable keeps the desired signer and leaves the applied state as it
        was, so the next call retries."""
        if (signer is self.request_signer and self._root_interception_applied == (signer is not None)
                and not self._bridge_rebind_pending):
            return
        self.request_signer = signer
        loop = self._loop
        if loop is None or not loop.is_running():
            return  # applied at the next (re)attach
        try:
            asyncio.run_coroutine_threadsafe(self._apply_request_interception(), loop).result(timeout=timeout)
        except Exception as e:  # health: allow BLE001 -- CDP/plugin boundary fails open; exception text may carry secrets
            logger.warning("web bot auth: could not %s request interception (%s)",
                           "enable" if signer is not None else "disable", type(e).__name__)

    async def _apply_request_interception(self) -> None:
        # Browser-level (no session): pauses every request of every target — each redirect hop, subresource,
        # worker and new tab — before it leaves, so ``_sign_and_continue`` can add the Web Bot Auth headers.
        wanted = self.request_signer is not None
        if wanted:
            await self._cdp("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
            self._root_interception_applied = True
            # Chrome only routes an already-loaded document's requests through a root interceptor enabled after it
            # once its session's own Fetch is (re)enabled: re-apply the unchanged bridge interceptor to each session.
            # Every session is tried; any failure leaves the rebind pending for the next command to retry.
            failed = []
            for sid in dict.fromkeys([self._page_session_id, *sorted(self._signing_sessions)]):
                if sid:
                    try:
                        await self._enable_bridge_fetch(sid, strict=True)
                    except Exception as e:  # health: allow BLE001 -- keep each CDP session retryable without logging request details
                        failed.append(type(e).__name__)
            self._bridge_rebind_pending = bool(failed)
            if failed:
                logger.warning("web bot auth: request interception rebind failed on %d session(s) (%s); "
                               "retried at the next command", len(failed), ", ".join(sorted(set(failed))))
        else:
            await self._cdp("Fetch.disable")
            self._root_interception_applied = False
            self._bridge_rebind_pending = False

    async def _continue_request(self, request_id: str, headers: Optional[list[dict[str, str]]]) -> None:
        params: dict[str, Any] = {"requestId": request_id}
        if headers is not None:
            params["headers"] = headers
        try:
            error = (await self._cdp("Fetch.continueRequest", params, timeout=5.0)).get("error")
        except Exception as e:  # health: allow BLE001 -- a CDP failure must fall back to unsigned continuation
            error = type(e).__name__
        if error and headers is not None:
            # A refused header set must not leave the request paused: let it go unsigned.
            logger.warning("web bot auth: signed continue refused (%s); request continues unsigned", error)
            await self._continue_request(request_id, None)
        elif error:
            logger.debug("web bot auth: continueRequest failed: %s", error)

    async def _sign_and_continue(self, request_id: str, request: dict[str, Any], signer: Any) -> None:
        """Add the Web Bot Auth headers and let the request go. Any signing failure lets it go unsigned, logged."""
        from tools import browser_web_bot_auth as wba

        url = str(request.get("url") or "")
        headers = None
        if signer is not None and url.startswith(("http://", "https://")):
            try:
                signed = await asyncio.wait_for(asyncio.to_thread(signer.headers, url), timeout=wba.SIGNER_TIMEOUT_S)
                kept = [{"name": k, "value": str(v)} for k, v in (request.get("headers") or {}).items()
                        if k.lower() not in {h.lower() for h in wba.SIGNED_HEADERS}]
                headers = kept + [{"name": k, "value": v} for k, v in signed.items()]
            except Exception as e:  # health: allow BLE001 -- arbitrary plugin errors must not block browsing or leak credentials
                logger.warning("web bot auth: signing failed (%s); request continues unsigned", type(e).__name__)
        await self._continue_request(request_id, headers)

    async def _on_fetch_paused(self, params, session_id):
        if session_id is not None:
            if _is_dialog_bridge(str((params.get("request") or {}).get("url") or "")):
                return await DialogSupervisionMixin._on_fetch_paused(self, params, session_id)
            if params.get("requestId"):
                await self._cdp_quiet("Fetch.continueRequest", {"requestId": params["requestId"]},
                                      session_id=session_id, timeout=3.0, what="passthrough")
            return
        request_id = params.get("requestId")
        if not request_id:
            return
        request = params.get("request") or {}
        signer = None if _is_dialog_bridge(str(request.get("url") or "")) else self.request_signer
        task = asyncio.create_task(self._sign_and_continue(request_id, request, signer))
        self._signing_tasks.add(task)
        task.add_done_callback(self._signing_tasks.discard)
