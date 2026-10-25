"""Apply native HTTP chat picks through the gateway's session-scoped /model machinery."""
import asyncio

from gateway.slash_commands_model import _ModelSwitchContext
from hermes_constants import get_hermes_home


async def apply_native_model_lock(adapter, session_id, source, session_key):
    db = await adapter._ensure_session_db_async()
    if db is None:
        raise RuntimeError("session database unavailable for native model lock")
    config = await asyncio.to_thread(db.get_session_model_config, session_id)
    request = adapter._runtime_request_from_persisted_session_lock({"model_config": config}, {})
    if request is None:
        return  # A missing lock must not erase settings made through other surfaces.
    model = request["requested"]["model"]
    if not model:
        raise RuntimeError("native model lock requires a model")
    runner = adapter.gateway_runner
    async with runner._model_switch_lock():
        ctx = _ModelSwitchContext(session_key=session_key, source=source,
                                  config_path=get_hermes_home() / "config.yaml", persist_global=False)
        await asyncio.to_thread(ctx.read_config)
        provider, base_url = ctx.route_provider, ctx.current_base_url
        # Named custom providers may keep the endpoint only in their provider entry.
        if provider and provider.startswith("custom:"):
            custom = next((p for p in (ctx.custom_provs or []) if p.get("name") == provider[7:]), {})
            base_url = custom.get("base_url") or base_url
        if not provider:
            raise RuntimeError("native model lock requires a configured provider route")
        requested_provider = request["requested"].get("provider")
        if requested_provider and requested_provider != provider:
            raise RuntimeError("native model lock differs from the configured provider route")
        override = runner._session_model_override(session_key) or {}
        if (override.get("model"), override.get("provider"), override.get("base_url")) != (model, provider, base_url):
            # Explicit provider prevents a model id/alias from silently choosing another route.
            result, error = await runner._perform_model_switch(ctx, model, provider, source)
            if error is not None:
                raise RuntimeError("native model lock could not resolve the configured provider route")
            if (result.new_model != model or result.target_provider != provider
                    or (base_url and result.base_url != base_url)):
                raise RuntimeError("native model lock differs from the configured provider route")
            # Do not switch a running cached agent: /submit may be enqueuing its next turn.
            await runner._record_model_switch(result, ctx, source=source, one_turn=False, picker=False)
        options = request["runtime_options"]
        runner._set_session_reasoning_override(session_key, options.get("reasoning_config"))
        runner._set_session_service_tier_override(session_key, options.get("service_tier"))
