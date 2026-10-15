"""Register Harso's transformed vault-value read guard through upstream middleware."""
from tools.harso_vault_guard import protect


def register(ctx):
    ctx.register_middleware("tool_execution", protect)
