import json

from agent.redact import clear_vault_redaction_values, register_vault_redaction_value
from tools.registry import ToolRegistry


def test_registry_scrubs_before_truncation_and_scrubs_exception_logs(caplog):
    registry = ToolRegistry()
    secret = 'fixture-vault-value-123456'
    register_vault_redaction_value(secret)
    try:
        registry.register(name='harso_test', toolset='harso_test', schema={'name': 'harso_test'},
                          handler=lambda args: json.dumps({'secret': secret}), max_result_size_chars=1000)
        assert secret not in registry.dispatch('harso_test', {})
        def fail(args):
            raise RuntimeError(secret)
        registry.get_entry('harso_test').handler = fail
        assert secret not in registry.dispatch('harso_test', {})
        assert secret not in caplog.text
    finally:
        clear_vault_redaction_values()

