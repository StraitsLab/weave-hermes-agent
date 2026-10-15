"""Memory-only native session credentials for the gateway and API execution paths."""
import threading
from datetime import datetime
from typing import Optional
from agent.session_credential import SessionCredential
from gateway.session import SessionSource

class NativeSessionCredentials:
    def bind_session_credential(
        self, source: SessionSource, session_id: str, bearer: str,
        expires_at: datetime, provider_route_revision_id: str,
        *, raise_on_reset: bool = False,
    ) -> bool:
        """Bind one memory-only credential to an existing strict session."""
        with self._session_credential_lock:
            entry = self.session_store.bind_existing_session(
                source, session_id, reopen=False, raise_on_reset=raise_on_reset,
            )
            if entry is None:
                return False
            # A reset can replace the entry after strict binding returns.
            # Publish the holder only while that exact route still owns the key.
            with self.session_store._lock:
                current = self.session_store._entries.get(entry.session_key)
                if current is not entry or current.session_id != session_id:
                    if raise_on_reset:
                        from gateway.session import SessionResetRequired
                        raise SessionResetRequired("source_displaced")
                    return False
                conversation = self._session_state(entry.session_key).conversation
                holder = conversation.credential_holder
                if holder is None:
                    conversation.credential_holder = SessionCredential(bearer, expires_at)
                    conversation.credential_route_revision_id = provider_route_revision_id
                    return True
                return (
                    conversation.credential_route_revision_id == provider_route_revision_id
                    and holder.refresh(bearer, expires_at)
                )
    def session_credential_available(
        self, source: SessionSource, session_id: str,
    ) -> bool:
        """Return whether this exact strict session has a usable holder."""
        return self.session_credential_for(source, session_id) is not None
    def session_credential_for(
        self, source: SessionSource, session_id: str,
    ) -> Optional[SessionCredential]:
        """Return the live callable, never a bearer snapshot or another session's key."""
        with self._session_credential_lock:
            entry = self.session_store._entries.get(
                self.session_store._generate_session_key(source)
            )
            if entry is None or entry.session_id != session_id:
                return None
            state = self._peek_session_state(entry.session_key)
            holder = state.conversation.credential_holder if state is not None else None
            if holder is None:
                return None
            try:
                holder()
            except RuntimeError:
                return None
            return holder
    def revoke_session_credential(self, source: SessionSource, session_id: str) -> None:
        """Revoke the holder for one exact strict session, if present."""
        with self._session_credential_lock:
            entry = self.session_store._entries.get(
                self.session_store._generate_session_key(source)
            )
            if entry is None or entry.session_id != session_id:
                return
            state = self._peek_session_state(entry.session_key)
            if state is not None and state.conversation.credential_holder is not None:
                state.conversation.credential_holder.revoke()
                state.conversation.credential_holder = None
                state.conversation.credential_route_revision_id = None

def install(runner):
    if runner is None or not hasattr(runner, "_peek_session_state") or hasattr(runner, "_session_credential_lock"):
        return
    runner._session_credential_lock = threading.RLock()
    for name, method in NativeSessionCredentials.__dict__.items():
        if callable(method) and not name.startswith("__"):
            setattr(runner, name, method.__get__(runner))


def runtime(runner, source, session_key, user_config):
    """Return the Gate-B route with a live holder; never fall back to ambient credentials."""
    from gateway.config import Platform
    if source is None or source.platform != Platform.API_SERVER:
        return None
    config = user_config or {}
    model = config.get("model") or {}
    provider = model.get("provider", "") if isinstance(model, dict) else ""
    if provider not in ("custom:weave-gate-b", "weave-gate-b"):
        install(runner)
        key = session_key or runner.session_store._generate_session_key(source)
        entry = runner.session_store._entries.get(key)
        holder = runner.session_credential_for(source, entry.session_id) if entry else None
        if holder is None:
            return None
        from gateway.run import _resolve_runtime_agent_kwargs, _resolve_gateway_model
        kwargs = _resolve_runtime_agent_kwargs()
        kwargs['api_key'], kwargs['credential_pool'] = holder, None
        return _resolve_gateway_model(config), kwargs
    install(runner)
    key = session_key or runner.session_store._generate_session_key(source)
    entry = runner.session_store._entries.get(key)
    holder = runner.session_credential_for(source, entry.session_id) if entry else None
    if holder is None:
        from gateway.native_bot_admission import enabled as native_bot_sessions
        if native_bot_sessions():
            return None  # native cells mint their own key through the profile's key_cmd provider
        raise RuntimeError("credential unavailable")
    candidates = config.get("custom_providers") or []
    if isinstance(candidates, dict):
        candidates = [dict(value, name=name) for name, value in candidates.items()]
    route = next((p for p in candidates if p.get("name") in ("weave-gate-b", provider)), None)
    if not route or not route.get("base_url"):
        raise RuntimeError("credential route unavailable")
    return model.get("default", ""), {"provider": provider, "base_url": route["base_url"],
        "api_key": holder, "credential_pool": None}


def for_api(adapter, session_id, requested_provider=None, route=None):
    from gateway.run import _load_gateway_config
    from gateway.platforms.api_server import _ProviderAuthResolutionError, _api_request_profile
    from gateway.config import Platform
    config = _load_gateway_config()
    provider = (config.get('model') or {}).get('provider')
    if provider not in ('custom:weave-gate-b', 'weave-gate-b'):
        return None
    source = SessionSource(platform=Platform.API_SERVER, chat_id=session_id or '', chat_type='dm',
                           user_id='api_server', user_name='API server', profile=_api_request_profile.get() or None)
    try:
        selected = runtime(adapter.gateway_runner, source, None, config)
        if adapter.gateway_runner.session_credential_for(source, session_id) is None:
            raise RuntimeError("credential unavailable")
    except Exception:  # health: allow BLE001,S110 -- HTTP/credential boundary: return typed refusal without exception contents.
        raise _ProviderAuthResolutionError('credential unavailable') from None
    model, kwargs = selected
    from gateway.run import _resolve_gateway_model
    if not _resolve_gateway_model():
        raise _ProviderAuthResolutionError('model unavailable')
    if (requested_provider and requested_provider != provider) or (route and (
        route.get('provider', provider) != provider or route.get('base_url', kwargs['base_url']) != kwargs['base_url'])):
        raise _ProviderAuthResolutionError('credential route mismatch')
    if route and route.get('model'):
        model = route['model']
    return model, kwargs


def revoke(adapter, session_id):
    from gateway.config import Platform
    from gateway.platforms.api_server import _api_request_profile
    runner = adapter.gateway_runner
    if runner is None:
        return
    install(runner)
    source = SessionSource(platform=Platform.API_SERVER, chat_id=session_id, chat_type='dm',
                           user_id='api_server', user_name='API server', profile=_api_request_profile.get() or None)
    if callable(getattr(runner, 'revoke_session_credential', None)):
        runner.revoke_session_credential(source, session_id)

