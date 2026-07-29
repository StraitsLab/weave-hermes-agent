"""Fail-closed control seam for execution handles owned by Hermes.

This module is intentionally not a model tool. Product integrations may wrap
``manage_execution`` at a profile-gated plugin edge, while Hermes remains the
sole owner of execution state and mutations.

Only public handles that Hermes can resolve authoritatively without another
registry are supported:

* ``delegate_task(background=true)`` delegation ids
* durable Kanban task ids, searched across existing boards

TUI session ids, transient subagent ids, and API run ids are transport-local
and are deliberately not guessed or mirrored here.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


_EXECUTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_STATE_TOKEN_RE = re.compile(r"^xec1_[0-9a-f]{32}$")
_ACTIONS = frozenset({"status", "steer", "interrupt", "cancel", "queue"})
_KANBAN_TERMINAL_STATES = frozenset({"done", "archived"})


@dataclass(frozen=True)
class _Target:
    backend: str
    state: str
    execution_id: str = ""
    authority: str = ""
    board: Optional[str] = None
    db_path: Optional[Path] = None
    title: str = ""
    revision: int = 0
    current_run_id: Optional[int] = None
    claim_lock: Optional[str] = None
    latest_event_id: int = 0
    control_action: str = ""
    control_request_id: str = ""
    live_controllable: bool = False


def _state_token(target: _Target) -> str:
    material = json.dumps(
        {
            "backend": target.backend,
            "execution_id": target.execution_id,
            "authority": target.authority,
            "state": target.state,
            "revision": target.revision,
            "run": target.current_run_id,
            "claim": target.claim_lock,
            "event": target.latest_event_id,
            "control": target.control_action,
            "live_controllable": target.live_controllable,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "xec1_" + hashlib.sha256(material).hexdigest()[:32]


def _refusal(
    code: str,
    message: str,
    *,
    execution_id: str = "",
    action: str = "",
    state: str = "",
    backend: str = "",
    allowed_actions: Optional[list[str]] = None,
    **extra: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "execution_id": execution_id,
        "action": action,
        "error": {"code": code, "message": message},
    }
    if state:
        result["state"] = state
    if backend:
        result["backend"] = backend
    if allowed_actions is not None:
        result["allowed_actions"] = allowed_actions
    result.update(extra)
    return result


def _allowed_actions(target: _Target) -> list[str]:
    if target.backend == "async_delegation":
        if target.state == "running" and target.live_controllable:
            return ["status", "cancel"]
        return ["status"]
    if target.backend == "kanban":
        if target.state == "running":
            return ["status", "interrupt", "cancel"]
        if target.state not in _KANBAN_TERMINAL_STATES:
            return ["status", "cancel"]
    return ["status"]


def _existing_kanban_paths() -> list[tuple[str, Path]]:
    """Return existing board DBs once each, without creating an empty board."""
    from hermes_cli import kanban_db as kb

    paths: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for meta in kb.list_boards(include_archived=True):
        board = str(meta.get("slug") or "default")
        path = Path(str(meta.get("db_path") or kb.kanban_db_path(board=board)))
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        paths.append((board, path))
    return paths


def _resolve(execution_id: str) -> list[_Target]:
    targets: list[_Target] = []

    from tools.async_delegation import (
        get_durable_delegation,
        is_async_delegation_controllable,
    )

    delegation = get_durable_delegation(execution_id)
    if delegation is not None:
        control_action = str(delegation.get("control_action") or "")
        state = str(delegation.get("state") or "unknown")
        if state == "running" and control_action:
            state = f"{control_action}_requested"
        targets.append(
            _Target(
                backend="async_delegation",
                state=state,
                execution_id=execution_id,
                authority=f"async:{delegation.get('authority') or 'state.db'}",
                revision=int(delegation.get("revision") or 1),
                control_action=control_action,
                control_request_id=str(
                    delegation.get("control_request_id") or ""
                ),
                live_controllable=is_async_delegation_controllable(
                    execution_id
                ),
            )
        )

    from hermes_cli import kanban_db as kb

    for board, path in _existing_kanban_paths():
        conn = kb.connect(db_path=path)
        try:
            task = kb.get_task(conn, execution_id)
            event_row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM task_events WHERE task_id=?",
                (execution_id,),
            ).fetchone()
        finally:
            conn.close()
        if task is not None:
            targets.append(
                _Target(
                    backend="kanban",
                    state=str(task.status),
                    execution_id=execution_id,
                    authority=f"kanban:{path.resolve()}",
                    board=board,
                    db_path=path,
                    title=str(task.title or ""),
                    current_run_id=task.current_run_id,
                    claim_lock=task.claim_lock,
                    latest_event_id=int(event_row[0] if event_row else 0),
                )
            )
    return targets


def _status_result(execution_id: str, target: _Target) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": True,
        "execution_id": execution_id,
        "action": "status",
        "backend": target.backend,
        "state": target.state,
        "state_token": _state_token(target),
        "allowed_actions": _allowed_actions(target),
        "idempotent": True,
    }
    if target.backend == "kanban":
        result["title"] = target.title
    return result


def _kanban_control_event(
    conn: Any, task_id: str, request_id: str
) -> Optional[tuple[str, str, str]]:
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id=? "
        "ORDER BY id DESC",
        (task_id,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("request_id") != request_id:
            continue
        action = str(payload.get("control_action") or "")
        if action:
            return action, str(row["kind"]), str(payload.get("reason") or "")
    return None


def _fresh_kanban_target(target: _Target, task_id: str) -> Optional[_Target]:
    from hermes_cli import kanban_db as kb

    if target.db_path is None:
        return None
    conn = kb.connect(db_path=target.db_path)
    try:
        task = kb.get_task(conn, task_id)
        event_row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM task_events WHERE task_id=?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    if task is None:
        return None
    return _Target(
        backend="kanban",
        state=str(task.status),
        execution_id=task_id,
        authority=target.authority,
        board=target.board,
        db_path=target.db_path,
        title=str(task.title or ""),
        current_run_id=task.current_run_id,
        claim_lock=task.claim_lock,
        latest_event_id=int(event_row[0] if event_row else 0),
    )


def _control_kanban(
    execution_id: str,
    target: _Target,
    *,
    action: str,
    expected_state: str,
    request_id: str,
    instruction: str,
    signal_fn: Any = None,
) -> dict[str, Any]:
    from hermes_cli import kanban_db as kb

    if target.db_path is None:
        return _refusal(
            "backend_unavailable",
            "Kanban database is unavailable.",
            execution_id=execution_id,
            action=action,
            state=target.state,
            backend=target.backend,
        )

    conn = kb.connect(db_path=target.db_path)
    try:
        task = kb.get_task(conn, execution_id)
        if task is None:
            return _refusal(
                "state_changed",
                "The task disappeared before the mutation could be applied.",
                execution_id=execution_id,
                action=action,
                backend=target.backend,
            )
        state = str(task.status)
        event_row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM task_events WHERE task_id=?",
            (execution_id,),
        ).fetchone()
        latest_event_id = int(event_row[0] if event_row else 0)
        current_target = _Target(
            backend="kanban",
            state=state,
            execution_id=execution_id,
            authority=target.authority,
            board=target.board,
            db_path=target.db_path,
            title=str(task.title or ""),
            current_run_id=task.current_run_id,
            claim_lock=task.claim_lock,
            latest_event_id=latest_event_id,
        )
        prior = _kanban_control_event(conn, execution_id, request_id)
        if prior is not None:
            prior_action, _kind, prior_reason = prior
            requested_reason = instruction or (
                "interrupted by user"
                if action == "interrupt"
                else "cancelled by user"
            )
            if prior_action != action or prior_reason != requested_reason:
                return _refusal(
                    "idempotency_conflict",
                    "request_id was already used for a different action.",
                    execution_id=execution_id,
                    action=action,
                    state=state,
                    backend=target.backend,
                    allowed_actions=_allowed_actions(
                        _Target(backend="kanban", state=state)
                    ),
                )
            return {
                "ok": True,
                "execution_id": execution_id,
                "action": action,
                "backend": target.backend,
                "state": state,
                "state_token": _state_token(current_target),
                "allowed_actions": _allowed_actions(
                    _Target(backend="kanban", state=state)
                ),
                "idempotent": True,
            }

        if _state_token(current_target) != expected_state:
            return _refusal(
                "stale_state_token",
                "Authoritative execution revision no longer matches expected_state.",
                execution_id=execution_id,
                action=action,
                state=state,
                backend=target.backend,
                allowed_actions=_allowed_actions(
                    _Target(backend="kanban", state=state)
                ),
                state_token=_state_token(current_target),
            )

        if action == "interrupt":
            if state != "running" or not task.claim_lock:
                return _refusal(
                    "unsupported_state",
                    "Kanban interrupt requires an identifiable running worker.",
                    execution_id=execution_id,
                    action=action,
                    state=state,
                    backend=target.backend,
                    allowed_actions=_allowed_actions(
                        _Target(backend="kanban", state=state)
                    ),
                )
            changed = kb.reclaim_task(
                conn,
                execution_id,
                reason=instruction or "interrupted by user",
                signal_fn=signal_fn,
                require_terminated=True,
                expected_status=state,
                request_id=request_id,
                expected_run_id=task.current_run_id,
            )
            requested_state = "ready"
        else:
            if state in _KANBAN_TERMINAL_STATES:
                return _refusal(
                    "terminal_state",
                    "Terminal Kanban work cannot be cancelled.",
                    execution_id=execution_id,
                    action=action,
                    state=state,
                    backend=target.backend,
                    allowed_actions=["status"],
                )
            if state == "running" and not task.claim_lock:
                return _refusal(
                    "worker_not_identifiable",
                    "Running Kanban work has no authoritative worker claim.",
                    execution_id=execution_id,
                    action=action,
                    state=state,
                    backend=target.backend,
                    allowed_actions=_allowed_actions(
                        _Target(backend="kanban", state=state)
                    ),
                )
            changed = kb.cancel_task(
                conn,
                execution_id,
                reason=instruction or "cancelled by user",
                signal_fn=signal_fn,
                expected_status=state,
                request_id=request_id,
                expected_run_id=task.current_run_id,
            )
            requested_state = "archived"
    finally:
        conn.close()

    fresh = _fresh_kanban_target(target, execution_id)
    fresh_state = fresh.state if fresh is not None else "missing"
    if not changed:
        verify = kb.connect(db_path=target.db_path)
        try:
            winner = _kanban_control_event(verify, execution_id, request_id)
        finally:
            verify.close()
        requested_reason = instruction or (
            "interrupted by user"
            if action == "interrupt"
            else "cancelled by user"
        )
        if (
            winner is not None
            and winner[0] == action
            and winner[2] == requested_reason
            and fresh is not None
        ):
            return {
                "ok": True,
                "execution_id": execution_id,
                "action": action,
                "backend": target.backend,
                "state": fresh.state,
                "state_token": _state_token(fresh),
                "allowed_actions": _allowed_actions(fresh),
                "idempotent": True,
            }
        # Completion and control both use compare-and-set mutations. If the
        # state moved, completion won; if it did not, worker termination could
        # not be proven and the task was deliberately left untouched.
        code = "state_changed" if fresh_state != state else "worker_signal_failed"
        message = (
            "Authoritative state changed before control committed."
            if code == "state_changed"
            else "The worker could not be proven terminated; task state was unchanged."
        )
        return _refusal(
            code,
            message,
            execution_id=execution_id,
            action=action,
            state=fresh_state,
            backend=target.backend,
            state_token=(_state_token(fresh) if fresh is not None else ""),
            allowed_actions=(
                _allowed_actions(fresh) if fresh is not None else ["status"]
            ),
        )

    return {
        "ok": True,
        "execution_id": execution_id,
        "action": action,
        "backend": target.backend,
        "state": fresh_state or requested_state,
        "state_token": (_state_token(fresh) if fresh is not None else ""),
        "allowed_actions": (
            _allowed_actions(fresh) if fresh is not None else ["status"]
        ),
        "idempotent": False,
    }


def _control_async_delegation(
    execution_id: str,
    target: _Target,
    *,
    action: str,
    expected_state: str,
    request_id: str,
    instruction: str,
) -> dict[str, Any]:
    from tools.async_delegation import control_async_delegation

    current_token = _state_token(target)
    is_duplicate = (
        target.control_action == action
        and target.control_request_id == request_id
    )
    if not is_duplicate and expected_state != current_token:
        return _refusal(
            "stale_state_token",
            "Authoritative execution revision no longer matches expected_state.",
            execution_id=execution_id,
            action=action,
            state=target.state,
            state_token=current_token,
            backend=target.backend,
            allowed_actions=_allowed_actions(target),
        )
    native = control_async_delegation(
        execution_id,
        action=action,
        request_id=request_id,
        expected_revision=target.revision,
        reason=instruction,
    )
    fresh_matches = [
        item for item in _resolve(execution_id)
        if item.backend == "async_delegation"
    ]
    fresh = fresh_matches[0] if len(fresh_matches) == 1 else target
    state = str(fresh.state)
    state_token = _state_token(fresh)
    if native.get("ok"):
        return {
            "ok": True,
            "execution_id": execution_id,
            "action": action,
            "backend": target.backend,
            "state": state,
            "state_token": state_token,
            "allowed_actions": (
                _allowed_actions(fresh)
            ),
            "idempotent": bool(native.get("idempotent")),
        }

    code = str(native.get("error") or "control_failed")
    messages = {
        "stale_state_token": "Authoritative execution revision changed.",
        "not_live_in_this_process": (
            "Delegation is durable but its owning process is not available."
        ),
        "not_running": "Delegation is no longer running.",
        "control_already_requested": "A different control request already won.",
        "idempotency_conflict": (
            "request_id was already used with a different control payload."
        ),
        "state_changed": "Authoritative state changed before control committed.",
        "signal_failed": "Worker signal failed; cancel intent was not committed.",
        "not_interruptible": "Delegation has no native interrupt callback.",
    }
    return _refusal(
        code,
        messages.get(code, "Native delegation control refused the request."),
        execution_id=execution_id,
        action=action,
        state=state,
        state_token=state_token,
        backend=target.backend,
        allowed_actions=_allowed_actions(fresh),
        action_committed=bool(native.get("action_committed")),
        signal_applied=native.get("signal_applied"),
    )


def manage_execution(
    execution_id: str,
    action: str,
    instruction: Optional[str] = None,
    expected_state: Optional[str] = None,
    request_id: Optional[str] = None,
    *,
    signal_fn: Any = None,
) -> dict[str, Any]:
    """Resolve and control one authoritative Hermes execution.

    ``signal_fn`` is dependency injection for focused tests of Kanban worker
    signalling. Product callers should omit it.
    """
    raw_id = execution_id if isinstance(execution_id, str) else ""
    execution_id = raw_id.strip()
    action = str(action or "").strip().lower()
    expected_state = str(expected_state or "").strip()
    request_id = str(request_id or "").strip()
    instruction = str(instruction or "").strip()

    if not execution_id or execution_id != raw_id or not _EXECUTION_ID_RE.fullmatch(execution_id):
        return _refusal(
            "invalid_execution_id",
            "execution_id is malformed.",
            execution_id=execution_id,
            action=action,
        )
    if action not in _ACTIONS:
        return _refusal(
            "invalid_action",
            "action must be status, steer, interrupt, cancel, or queue.",
            execution_id=execution_id,
            action=action,
        )
    if action != "status" and not _REQUEST_ID_RE.fullmatch(request_id):
        return _refusal(
            "invalid_request_id",
            "A stable request_id is required for mutating actions.",
            execution_id=execution_id,
            action=action,
        )
    if action != "status" and not expected_state:
        return _refusal(
            "expected_state_required",
            "expected_state is required for mutating actions.",
            execution_id=execution_id,
            action=action,
        )
    if expected_state and not _STATE_TOKEN_RE.fullmatch(expected_state):
        return _refusal(
            "invalid_expected_state",
            "expected_state is malformed.",
            execution_id=execution_id,
            action=action,
        )
    if len(instruction) > 4000:
        return _refusal(
            "invalid_instruction",
            "instruction exceeds 4000 characters.",
            execution_id=execution_id,
            action=action,
        )

    targets = _resolve(execution_id)
    if not targets:
        return _refusal(
            "unknown_execution_id",
            "No authoritative Hermes execution matches this id.",
            execution_id=execution_id,
            action=action,
        )
    if len(targets) != 1:
        return _refusal(
            "ambiguous_execution_id",
            "More than one authoritative execution matches this id.",
            execution_id=execution_id,
            action=action,
            matches=len(targets),
        )
    target = targets[0]
    allowed = _allowed_actions(target)

    if action == "status":
        token = _state_token(target)
        if expected_state and token != expected_state:
            return _refusal(
                "stale_state_token",
                "Authoritative execution revision no longer matches expected_state.",
                execution_id=execution_id,
                action=action,
                state=target.state,
                backend=target.backend,
                allowed_actions=allowed,
                state_token=token,
            )
        return _status_result(execution_id, target)

    supported_mutations = (
        {"cancel"}
        if target.backend == "async_delegation"
        else {"interrupt", "cancel"}
    )
    if action not in supported_mutations:
        reason = (
            "queue is a native future session turn; it is not a Kanban "
            "dependency or async-delegation operation."
            if action == "queue"
            else f"{action} is not natively supported for {target.backend} "
            f"in state {target.state}."
        )
        return _refusal(
            "unsupported_action",
            reason,
            execution_id=execution_id,
            action=action,
            state=target.state,
            backend=target.backend,
            allowed_actions=allowed,
        )

    # Resolve again immediately before dispatch; each backend then performs an
    # authoritative revision/claim CAS. No process-local lock is the authority.
    fresh_targets = _resolve(execution_id)
    if len(fresh_targets) != 1:
        return _refusal(
            "state_changed",
            "Execution identity changed before the mutation could run.",
            execution_id=execution_id,
            action=action,
        )
    target = fresh_targets[0]
    if target.backend == "async_delegation":
        return _control_async_delegation(
            execution_id,
            target,
            action=action,
            expected_state=expected_state,
            request_id=request_id,
            instruction=instruction,
        )
    return _control_kanban(
        execution_id,
        target,
        action=action,
        expected_state=expected_state,
        request_id=request_id,
        instruction=instruction,
        signal_fn=signal_fn,
    )
