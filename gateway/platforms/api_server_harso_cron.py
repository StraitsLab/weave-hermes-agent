"""Harso routine HTTP contract layered on upstream cron admission."""
from __future__ import annotations
import asyncio
import logging
import threading
from contextlib import suppress
from functools import partial
from typing import Optional
from aiohttp import web
from gateway.platforms.api_server import (
    _reserve_pending_api_work, _release_pending_api_work,
    _redact_api_error_text, _call_verifier,
)
from cron.jobs import (
    get_job as _cron_get,
    remove_job_exact as _cron_remove, pause_job_exact as _cron_pause,
    resume_job_exact as _cron_resume,
)
from cron.scheduler import create_job_with_scheduler_registration as _cron_create
from tools.cronjob_tools import _scan_cron_prompt
logger = logging.getLogger(__name__)

async def _run_thread_until_terminal(target, *args, abort_event: Optional[threading.Event] = None, **kwargs):
    worker = asyncio.create_task(asyncio.to_thread(target, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        if abort_event:
            abort_event.set()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("cron worker failed during cancellation")
        raise

def _detach_api_task(adapter, reservation, awaitable):
    task = asyncio.create_task(awaitable)
    reservation["detached"] = True
    task.add_done_callback(lambda _task: _release_pending_api_work(adapter, reservation))
    adapter._background_tasks.add(task)
    task.add_done_callback(adapter._background_tasks.discard)

def _fire_claim_kwargs(provider, adapters, loop):
    cancel_event = threading.Event()
    kwargs = {"adapters": adapters, "loop": loop}
    from cron.scheduler_provider import provider_supports_fire_cancel
    if provider_supports_fire_cancel(provider):
        kwargs["cancel_event"] = cancel_event
    return kwargs, cancel_event

def _dispatch_claimed_api_fire(
    provider, claimed_job, adapter, reservation, adapters, loop, *, extra_prompt=None,
):
    """Hand one admitted claim to the provider's dispatch rail.

    ``extra_prompt`` is the transient per-run context parsed by
    ``_handle_run_job`` (the same body ``prompt`` upstream stamps via
    ``trigger_job(extra_prompt=...)``). It rides on the claimed snapshot for
    this fire only and is never persisted into the job definition.
    """
    fire_kwargs, cancel_event = _fire_claim_kwargs(provider, adapters, loop)

    def submit(worker):
        _detach_api_task(
            adapter,
            reservation,
            _run_thread_until_terminal(worker, abort_event=cancel_event),
        )

    dispatch = getattr(provider, "dispatch_claimed_fire", None)
    if callable(dispatch):
        if extra_prompt:
            return dispatch(
                claimed_job, **fire_kwargs, submit=submit, extra_prompt=extra_prompt
            )
        return dispatch(claimed_job, **fire_kwargs, submit=submit)
    if extra_prompt:
        # Base ``fire_claimed`` reads the transient context off the snapshot.
        claimed_job = dict(claimed_job, _cron_extra_prompt=extra_prompt)
    submit(lambda: provider.fire_claimed(claimed_job, **fire_kwargs))
    return True

def _abort_claimed_api_fire(provider, claimed_job, error):
    abort = getattr(provider, "abort_claimed_fire", None)
    if callable(abort):
        abort(claimed_job, error)

def _script_error(script, no_agent):
    if not isinstance(no_agent, bool):
        return "no_agent must be a boolean"
    if no_agent and script is None:
        return "no_agent=True requires a script"
    if script is None:
        return None
    from cron.harso_scripts import PLATFORM_SCRIPT_PREFIX
    from tools.cronjob_tools import _validate_cron_script_path
    if not isinstance(script, str) or not script.startswith(PLATFORM_SCRIPT_PREFIX):
        return "script must be a platform: reference"
    return _validate_cron_script_path(script)


async def _handle_create_job(self, request: web.Request) -> web.Response:
    """POST /api/jobs — create a new cron job."""
    _, error = self._cron_request_guard(request)
    if error is not None:
        return error
    from cron.jobs import CronDedupConflict, CronDedupKeyInvalid, CronStoreLockUnavailable
    from cron.scheduler import CronSchedulerRegistrationError
    try:
        body = await request.json()
        name = (body.get("name") or "").strip()
        schedule = (body.get("schedule") or "").strip()
        prompt = body.get("prompt", "")
        deliver = body.get("deliver", "local")
        skills = body.get("skills")
        repeat = body.get("repeat")
        dedup_key = body.get("dedup_key")
        script = body.get("script")
        no_agent = body.get("no_agent", False)

        if not name:
            return web.json_response({"error": "Name is required"}, status=400)
        if len(name) > self._MAX_NAME_LENGTH:
            return web.json_response(
                {"error": f"Name must be ≤ {self._MAX_NAME_LENGTH} characters"}, status=400,
            )
        if not schedule:
            return web.json_response({"error": "Schedule is required"}, status=400)
        prompt_error = self._validate_cron_prompt(prompt)
        if prompt_error is not None:
            return prompt_error
        if repeat is not None and (not isinstance(repeat, int) or repeat < 1):
            return web.json_response({"error": "Repeat must be a positive integer"}, status=400)
        script_error = _script_error(script, no_agent)
        if script_error:
            return web.json_response({"error": script_error}, status=400)
        kwargs = {
            "prompt": prompt,
            "schedule": schedule,
            "name": name,
            "deliver": deliver,
            "origin": self._cron_origin_from_request(request),
        }
        for key in ("paused", "paused_reason", "model", "provider", "base_url", "reasoning_effort"):
            if key in body:
                kwargs[key] = body[key]
        if skills:
            kwargs["skills"] = skills
        kwargs.update({key: value for key, value in (
            ("repeat", repeat), ("dedup_key", dedup_key), ("script", script),
        ) if value is not None})
        if no_agent:
            kwargs["no_agent"] = True

        job = _cron_create(**kwargs)
        return web.json_response({"job": job})
    except CronStoreLockUnavailable:
        return web.json_response({"error": "cron store lock unavailable", "retryable": True}, status=503)
    except CronDedupKeyInvalid as e:
        return web.json_response({"error": str(e)}, status=400)
    except CronDedupConflict as e:
        return web.json_response({"error": str(e), "job_id": e.job_id}, status=409)
    except CronSchedulerRegistrationError as e:
        return web.json_response(e.to_dict(), status=424)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except Exception as e:
        logger.exception("cron HTTP request failed")
        return web.json_response({"error": _redact_api_error_text(e)}, status=500)

async def _handle_run_job(self, request: web.Request) -> web.Response:
    """POST /api/jobs/{job_id}/run — admit and dispatch one native run."""
    job_id, error = self._cron_request_guard(request, need_job_id=True, check_draining=True)
    if error is not None:
        return error
    extra_prompt = None
    respect_pause = False
    try:
        body = await request.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        # Opt-in non-forcing admission (arrival links): a paused job is
        # refused atomically under the claim lock instead of resumed.
        respect_pause = body.get("respect_pause", False)
        if not isinstance(respect_pause, bool):
            return web.json_response({"error": "respect_pause must be a boolean"}, status=400)
        raw_prompt = body.get("prompt")
        if raw_prompt is not None:
            extra_prompt = str(raw_prompt)
            if len(extra_prompt) > self._MAX_PROMPT_LENGTH:
                return web.json_response(
                    {"error": f"Prompt must be ≤ {self._MAX_PROMPT_LENGTH} characters"},
                    status=400,
                )
            if extra_prompt and _scan_cron_prompt is not None:
                scan_error = _scan_cron_prompt(extra_prompt)
                if scan_error:
                    return web.json_response({"error": scan_error}, status=400)
            extra_prompt = extra_prompt or None
    with _reserve_pending_api_work(self) as reservation:
        try:
            job = _cron_get(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)

            from cron.executions import execution_projection, latest_execution
            from cron.scheduler_provider import provider_supports_claim_force, provider_supports_split_fire, resolve_cron_scheduler
            provider = resolve_cron_scheduler()
            provider_source = str(getattr(provider, "name", "provider"))
            if not provider_supports_split_fire(provider): return web.json_response({"error": "cron provider does not support typed run identity", "job_id": job_id}, status=409)
            previous_execution = latest_execution(job_id)
            claim_kwargs = {"force": True} if provider_supports_claim_force(provider) and not respect_pause else {}
            from cron.scheduler_provider import provider_claim_accepts
            if provider_claim_accepts(provider, "manual"):
                claim_kwargs["manual"] = True
            try:
                claimed_job = await asyncio.to_thread(provider.claim_fire, job_id, **claim_kwargs)
            except Exception:
                logger.exception("cron API run admission failed for %s", job_id)
                return web.json_response({"error": "cron run admission failed", "job_id": job_id}, status=503)
            execution_id = (claimed_job.get("execution_id") or
                            (claimed_job.get("execution") or {}).get("id")) if isinstance(claimed_job, dict) else None
            if claimed_job is None:
                job = _cron_get(job_id)
                if not job:
                    return web.json_response({"error": "Job not found"}, status=404)
                latest = latest_execution(job_id)
                previous_id = previous_execution.get("id") if isinstance(previous_execution, dict) else None
                fresh_execution = bool(isinstance(latest, dict) and latest.get("id") and latest.get("id") != previous_id)
                execution = execution_projection(
                    latest.get("id") if fresh_execution else None,
                    job_id=job_id,
                    source=provider_source,
                    status=latest.get("status", "unknown") if latest else "unknown",
                )
                return web.json_response(
                    {"job": job, "execution": execution,
                     "status": "duplicate" if fresh_execution else "not_admitted"}, status=200,
                )

            try:
                loop = asyncio.get_running_loop()
                runner = self.gateway_runner or request.app.get("gateway_runner")
                adapters = getattr(runner, "adapters", None) or None
                dispatched = _dispatch_claimed_api_fire(
                    provider, claimed_job, self, reservation, adapters, loop,
                    extra_prompt=extra_prompt,
                )
            except BaseException as setup_error:
                _abort_claimed_api_fire(
                    provider,
                    claimed_job, str(setup_error) or type(setup_error).__name__
                )
                raise
            if not dispatched:
                return web.json_response(
                    {"error": "cron run dispatch failed", "job_id": job_id},
                    status=503,
                )
            execution = execution_projection(execution_id, job_id=job_id,
                                              source=provider_source, status="claimed")
            return web.json_response(
                {"job": job, "execution": execution},
                status=202,
            )
        except Exception as e:
            logger.exception("cron HTTP dispatch failed")
            return web.json_response({"error": _redact_api_error_text(e)}, status=500)

async def _handle_list_job_runs(self, request: web.Request) -> web.Response:
    job_id, error = self._cron_request_guard(request, need_job_id=True)
    if error is not None:
        return error
    raw_limit = request.query.get("limit", "50")
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        return web.json_response({"error": "limit must be an integer"}, status=400)
    if limit < 1:
        return web.json_response({"error": "limit must be positive"}, status=400)
    cursor = request.query.get("before_claimed_at") or None
    try:
        if not _cron_get(job_id):
            return web.json_response({"error": "Job not found"}, status=404)
        from cron.executions import list_execution_page
        runs, next_cursor, has_more = list_execution_page(job_id, limit=limit, before_claimed_at=cursor)
        return web.json_response({"job_id": job_id, "runs": runs,
                                  "next_cursor": next_cursor, "has_more": has_more})
    except ValueError:
        return web.json_response({"error": "Malformed execution cursor"}, status=400)
    except Exception as e:
        logger.exception("cron HTTP request failed")
        return web.json_response({"error": _redact_api_error_text(e)}, status=500)

async def _mutate(adapter, request, action):
    job_id, err = adapter._cron_request_guard(request, need_job_id=True)
    if err is not None:
        return err
    return adapter._job_response(action, job_id, notify=True)


async def _delete(adapter, request):
    job_id, err = adapter._cron_request_guard(request, need_job_id=True)
    if err is not None:
        return err
    if not _cron_remove(job_id):
        return web.json_response({"error": "Job not found"}, status=404)
    from gateway.platforms.api_server import _notify_cron_provider_jobs_changed
    _notify_cron_provider_jobs_changed()
    return web.json_response({"ok": True})


def _http_routes(adapter):
    return [
        ("POST", "/api/cron/fire", partial(_handle_cron_fire, adapter)),
        ("DELETE", "/api/jobs/{job_id}", partial(_delete, adapter)),
        ("POST", "/api/jobs", partial(_handle_create_job, adapter)),
        ("POST", "/api/jobs/{job_id}/run", partial(_handle_run_job, adapter)),
        ("GET", "/api/jobs/{job_id}/runs", partial(_handle_list_job_runs, adapter)),
        ("POST", "/api/jobs/{job_id}/pause", partial(_mutate, adapter, action=_cron_pause)),
        ("POST", "/api/jobs/{job_id}/resume", partial(_mutate, adapter, action=_cron_resume)),
    ]


def merge_routes(routes, additions):
    """Replace only explicitly overridden method/path pairs, preserving upstream routes."""
    keys = {(method, path) for method, path, _ in additions}
    return [route for route in routes if route[:2] not in keys] + additions


async def _handle_cron_fire(self, request: web.Request) -> web.Response:
    """POST /api/cron/fire — Chronos fire webhook (NAS -> agent), authenticated by a
    NAS-minted JWT via the pluggable verifier, NOT API_SERVER_KEY. 202 + background run so
    a long turn never trips NAS's timeout; the store CAS claim guards double-fire on retry."""
    # Startup-wait budget starts here so a slow JWKS fetch cannot outlast the forwarder timeout.
    received_at = asyncio.get_running_loop().time()
    from hermes_cli.config import cfg_get, load_config
    from plugins.cron_providers.chronos.verify import get_fire_verifier
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.startswith("Bearer ") else ""
    cfg = await asyncio.to_thread(load_config)
    verifier = get_fire_verifier()
    verify_kwargs = dict(
        token=token,
        expected_audience=cfg_get(cfg, "cron", "chronos", "expected_audience", default=""),
        jwks_or_key=cfg_get(cfg, "cron", "chronos", "nas_jwks_url", default="") or None,
        issuer=cfg_get(cfg, "cron", "chronos", "portal_url", default="") or None)
    try:
        claims = await _call_verifier(verifier, **verify_kwargs)
    except Exception:
        # Fail closed: a crashing verifier must never admit a fire.
        logger.exception("cron fire: verifier crashed; rejecting token")
        claims = None
    if claims is None:
        logger.warning("cron fire: rejected invalid token: %s", self._request_audit_log_suffix(request))
        return web.json_response({"error": "invalid fire token"}, status=401)
    draining = self._draining_response()
    if draining is not None:
        return draining
    with _reserve_pending_api_work(self) as reservation:
        body = {}
        with suppress(Exception):
            body = await request.json()
        job_id = (body or {}).get("job_id")
        if not job_id:
            return web.json_response({"error": "missing job_id"}, status=400)
        # `hermes pause` ESTOP: refuse the fire and ask NAS to retry later.
        # Placed after JWT verify (don't leak pause state to unauth callers)
        # and after the drain check (drain is transient shutdown, ESTOP is
        # operator override). 503 + Retry-After reschedules the job via NAS
        # retry or the misfire backstop rather than silently dropping it —
        # matches _CRON_FIRE_RETRY_AFTER_SECONDS in web_routers/cron.py.
        with suppress(ImportError):
            from agent.estop import check_paused as _estop_check_paused
            if _estop_check_paused("cron-webhook", logger):
                return web.json_response(
                    {"error": "hermes is paused (ESTOP)", "job_id": job_id},
                    status=503,
                    headers={"Retry-After": str(60)},
                )
        from cron.scheduler_provider import provider_supports_split_fire, resolve_cron_scheduler
        provider = resolve_cron_scheduler()
        loop = asyncio.get_running_loop()
        from gateway.platforms.api_server_fire_startup import live_adapters_once_started
        refusal, adapters = await live_adapters_once_started(self, request, job_id, received_at=received_at)
        if refusal is not None:
            return refusal

        if not provider_supports_split_fire(provider):
            # A legacy single-phase provider overrides ``fire_due`` but inherits the base
            # ``claim_fire``; the split path would silently bypass that override.
            _detach_api_task(self, reservation, _run_thread_until_terminal(
                provider.fire_due, job_id, adapters=adapters, loop=loop))
            return web.json_response({"status": "accepted", "job_id": job_id}, status=202)
        # Persist the attempt + exact store owner before acknowledging NAS; a failure here
        # is retryable and the reservation remains attached.
        try:
            claimed_job = await asyncio.to_thread(provider.claim_fire, job_id)
        except Exception:
            logger.exception("cron fire admission failed for %s", job_id)
            return web.json_response({"error": "cron fire admission failed", "job_id": job_id}, status=503)
        if claimed_job is None:
            return web.json_response({"status": "duplicate", "job_id": job_id}, status=200)
        if not _dispatch_claimed_api_fire(provider, claimed_job, self, reservation, adapters, loop):
            return web.json_response({"error": "cron fire dispatch failed", "job_id": job_id}, status=503)
        return web.json_response({"status": "accepted", "job_id": job_id}, status=202)
