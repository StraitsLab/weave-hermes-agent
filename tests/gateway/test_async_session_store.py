"""Async SessionStore boundary for gateway event-loop safety."""

import ast
import asyncio
import threading
from pathlib import Path

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import AsyncSessionStore, SessionSource, SessionStore
from hermes_state import SessionDB


class _SpyStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.label = "store"

    def read(self, value: str) -> str:
        self.calls.append((value, threading.get_ident()))
        return value


def _nearest_function(node: ast.AST, parents: dict[ast.AST, ast.AST]):
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
    return None


def _is_awaited(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, ast.Await):
            return True
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return False
    return False


def test_gateway_async_code_uses_one_awaited_session_store_boundary() -> None:
    """Loop-side store calls must use the facade; raw store remains sync-only."""
    root = Path(__file__).resolve().parents[2]
    violations: list[str] = []
    for rel in ("gateway/run.py", "gateway/slash_commands.py"):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for owner in (node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)):
            raw_aliases = {
                target.id
                for node in ast.walk(owner)
                if isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id in {"self", "_self"}
                and node.value.attr == "session_store"
                for target in node.targets
                if isinstance(target, ast.Name)
            }
            for node in ast.walk(owner):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if _nearest_function(node, parents) is not owner:
                    # A nested sync helper (for example run_sync) executes off-loop.
                    continue
                receiver = node.func.value
                if isinstance(receiver, ast.Name) and receiver.id in raw_aliases:
                    violations.append(
                        f"{rel}:{node.lineno} raw alias {receiver.id}.{node.func.attr}() in async {owner.name}"
                    )
                    continue
                if not (
                    isinstance(receiver, ast.Attribute)
                    and isinstance(receiver.value, ast.Name)
                    and receiver.value.id in {"self", "_self"}
                ):
                    continue
                if receiver.attr == "session_store":
                    violations.append(
                        f"{rel}:{node.lineno} raw session_store.{node.func.attr}() in async {owner.name}"
                    )
                elif receiver.attr == "async_session_store" and not _is_awaited(
                    node, parents
                ):
                    violations.append(
                        f"{rel}:{node.lineno} unawaited async_session_store.{node.func.attr}()"
                    )
    assert not violations, "\n".join(violations)


def test_no_repository_local_claude_permissions_file() -> None:
    root = Path(__file__).resolve().parents[2]
    assert not (root / ".claude" / "settings.json").exists()


def test_bind_existing_session_never_creates_a_ghost_session(tmp_path, monkeypatch) -> None:
    """Direct API binding must publish only the requested existing session."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(tmp_path / "state.db")
    db.create_session("existing-api-session", "api_server")
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    store._db = db
    source = SessionSource(
        platform=Platform.API_SERVER, chat_id="existing-api-session",
        chat_type="dm", user_id="api_server",
    )

    entry = store.bind_existing_session(source, "existing-api-session")

    assert entry is not None
    assert entry.session_id == "existing-api-session"
    assert [row["id"] for row in db.list_sessions_rich(limit=20)] == [
        "existing-api-session"
    ]
    db.close()
