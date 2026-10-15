"""Harso ``cron_run_credential`` plugin hook, invoked once per native cron run at agent construction.

Kwargs (passed by signature): job_id, session_id, provider, base_url. Return an
``agent.session_credential.SessionCredential`` to replace the run's api_key, or None.
The first non-None answer wins; a raising hook fails the run closed.
"""
from __future__ import annotations


def apply_credential(job, session_id, runtime, setup):
    from hermes_cli.plugins import iter_hook_callbacks
    for hook in iter_hook_callbacks("cron_run_credential"):
        import inspect
        context = {"job_id": job.get("id"), "session_id": session_id,
                   "provider": runtime.get("provider"), "base_url": runtime.get("base_url")}
        parameters = inspect.signature(hook).parameters
        answer = hook(**{k: v for k, v in context.items() if k in parameters or
                        any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())})
        if answer is not None:
            from agent.session_credential import SessionCredential
            if not isinstance(answer, SessionCredential):
                raise TypeError("cron_run_credential must return SessionCredential or None")
            runtime = dict(runtime, api_key=answer)
            setup.credential_pool = None
            break
    return runtime
