"""An explicit return to the configured bare-custom route keeps model.api_key (Part of WEV-3567)."""
import pytest

from hermes_cli import model_switch as ms, runtime_provider as rp

def test_explicit_bare_custom_return_retains_configured_auth(tmp_path, monkeypatch):
    cfg = {'model': {'default': 'model-a', 'provider': 'custom',
                     'base_url': 'https://private.example/v1', 'api_key': 'configured-fixture-secret'}}
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr('hermes_cli.config.load_config', lambda *a, **kw: cfg)
    monkeypatch.setattr(rp, 'load_config', lambda *a, **kw: cfg)
    monkeypatch.setattr(rp, '_get_model_config', lambda: cfg['model'])
    monkeypatch.setattr(rp, '_getenv', lambda *a, **kw: '')
    monkeypatch.setattr(rp, '_try_resolve_from_custom_pool', lambda *a, **kw: None)
    monkeypatch.setattr(ms, 'DIRECT_ALIASES', {})
    monkeypatch.setattr(ms, 'get_model_info', lambda *a, **kw: None)
    monkeypatch.setattr(ms, 'get_model_capabilities', lambda *a, **kw: None)
    monkeypatch.setattr('hermes_cli.models.validate_requested_model', lambda *a, **kw:
                        {'accepted': True, 'persist': True, 'recognized': True, 'message': ''})
    startup = rp.resolve_runtime_provider()
    assert startup['base_url'] == cfg['model']['base_url']
    assert startup['api_key'] == cfg['model']['api_key']
    result = ms.switch_model('model-a', current_provider='custom', current_model='model-b',
                            current_base_url='https://other.example/v1', current_api_key='other-fixture-key',
                            explicit_provider='custom', user_providers={}, custom_providers=[])
    assert result.success
    assert result.base_url == startup['base_url']
    assert result.api_key == startup['api_key']



@pytest.mark.parametrize('env', [{}, {'OPENAI_BASE_URL': 'https://env-openai.example/v1'}, {'OPENROUTER_BASE_URL': 'https://env-router.example/v1'}, {'HERMES_INFERENCE_PROVIDER': 'openrouter'}, {'CUSTOM_BASE_URL': 'https://env-custom.example/v1'}])
def test_explicit_bare_custom_return_pins_configured_route_under_env(tmp_path, monkeypatch, env):
    cfg = {'model': {'default': 'model-a', 'provider': 'custom', 'base_url': 'https://private.example/v1', 'api_key': 'configured-fixture-secret'}, 'providers': {'competitor': {'base_url': 'https://competitor.example/v1', 'api_key': 'competitor-fixture-key'}}}
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr('hermes_cli.config.load_config', lambda *a, **kw: cfg)
    monkeypatch.setattr(rp, 'load_config', lambda *a, **kw: cfg)
    monkeypatch.setattr(rp, '_get_model_config', lambda: cfg['model'])
    monkeypatch.setattr(rp, '_getenv', lambda name, default='': env.get(name, default))
    monkeypatch.setattr(rp, 'load_pool', lambda *a, **kw: None)
    # Exercise real custom pool lookup, but avoid live auth store reads.
    monkeypatch.setattr('agent.credential_pool.load_pool', lambda *a, **kw: None)
    monkeypatch.setattr(ms, 'DIRECT_ALIASES', {})
    monkeypatch.setattr(ms, 'get_model_info', lambda *a, **kw: None)
    monkeypatch.setattr(ms, 'get_model_capabilities', lambda *a, **kw: None)
    monkeypatch.setattr('hermes_cli.models.validate_requested_model', lambda *a, **kw: {'accepted': True, 'persist': True, 'recognized': True, 'message': ''})
    startup = rp.resolve_runtime_provider()
    result = ms.switch_model('model-a', current_provider='custom', current_model='model-b', current_base_url='https://other.example/v1', current_api_key='other-fixture-key', explicit_provider='custom', user_providers={}, custom_providers=[])
    assert result.success
    assert result.base_url == cfg['model']['base_url']
    assert result.api_key == cfg['model']['api_key']
