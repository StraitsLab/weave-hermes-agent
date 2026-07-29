from __future__ import annotations

import threading
import time

import pytest

from hermes_cli import execution_control as ec
from hermes_cli import kanban_db as kb
from tools import async_delegation as ad
from tools.delegate_tool import _interrupt_async_children


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    ad._reset_for_tests()
    yield home
    deadline = time.monotonic() + 2
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.01)
    ad._reset_for_tests()


def _dispatch(*, interrupt_fn=None, runner=None) -> tuple[str, threading.Event]:
    gate = threading.Event()

    def default_runner():
        gate.wait(timeout=5)
        return {"status": "completed", "summary": "late result"}

    result = ad.dispatch_async_delegation(
        goal="controlled work",
        context=None,
        toolsets=None,
        role="leaf",
        model="test",
        session_key="session",
        runner=runner or default_runner,
        interrupt_fn=interrupt_fn or gate.set,
        max_async_children=3,
    )
    return result["delegation_id"], gate


def _wait_terminal(execution_id: str, timeout: float = 5) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        durable = ad.get_durable_delegation(execution_id)
        if durable and durable["state"] not in {"running", "finalizing"}:
            return durable
        time.sleep(0.01)
    raise AssertionError(f"{execution_id} did not become terminal")


def _create_task(*, title="task") -> str:
    kb.init_db()
    conn = kb.connect()
    try:
        return kb.create_task(conn, title=title)
    finally:
        conn.close()


def _token(execution_id: str) -> str:
    status = ec.manage_execution(execution_id, "status")
    assert status["ok"] is True
    return status["state_token"]


def _claim_task(task_id: str, pid: int = 99123) -> None:
    conn = kb.connect()
    try:
        assert kb.claim_task(conn, task_id) is not None
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, task_id))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("backend", "action", "supported"),
    [
        ("async", "status", True),
        ("async", "steer", False),
        ("async", "interrupt", False),
        ("async", "cancel", True),
        ("async", "queue", False),
        ("kanban", "status", True),
        ("kanban", "steer", False),
        ("kanban", "interrupt", True),
        ("kanban", "cancel", True),
        ("kanban", "queue", False),
    ],
)
def test_backend_action_compatibility_matrix(
    monkeypatch, backend, action, supported
):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    if backend == "async":
        execution_id, gate = _dispatch()
        expected = _token(execution_id)
    else:
        execution_id = _create_task()
        expected = _token(execution_id)
        gate = None
        if action == "interrupt":
            _claim_task(execution_id)
            expected = _token(execution_id)

    result = ec.manage_execution(
        execution_id,
        action,
        instruction="control",
        expected_state=expected,
        request_id=f"req-{backend}-{action}",
        signal_fn=lambda *_args: None,
    )
    assert result["ok"] is supported
    if not supported:
        assert result["error"]["code"] == "unsupported_action"
    if gate is not None:
        gate.set()


def test_status_is_authoritative_and_queue_is_not_a_dependency():
    task_id = _create_task(title="durable successor is separate")
    status = ec.manage_execution(task_id, "status")
    assert status["backend"] == "kanban"
    assert status["state"] == "ready"
    assert status["title"] == "durable successor is separate"

    refused = ec.manage_execution(
        task_id,
        "queue",
        instruction="this must not create a child card",
        expected_state=status["state_token"],
        request_id="queue-1",
    )
    assert refused["error"]["code"] == "unsupported_action"
    conn = kb.connect()
    try:
        assert len(kb.list_tasks(conn)) == 1
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("execution_id", "code"),
    [
        ("", "invalid_execution_id"),
        (" t_deadbeef", "invalid_execution_id"),
        ("../../state.db", "invalid_execution_id"),
        ("t_doesnotexist", "unknown_execution_id"),
    ],
)
def test_malformed_and_unknown_ids_fail_closed(execution_id, code):
    result = ec.manage_execution(execution_id, "status")
    assert result["ok"] is False
    assert result["error"]["code"] == code


def test_unknown_status_does_not_create_a_kanban_database(isolated_home):
    assert not (isolated_home / "kanban.db").exists()
    assert ec.manage_execution("t_missing", "status")["ok"] is False
    assert not (isolated_home / "kanban.db").exists()


def test_ambiguous_task_identity_across_boards(monkeypatch):
    monkeypatch.setattr(kb, "_new_task_id", lambda: "t_ambiguous")
    kb.create_board("one")
    kb.create_board("two")
    for board in ("one", "two"):
        conn = kb.connect(board=board)
        try:
            assert (
                kb.create_task(
                    conn,
                    title=board,
                    board=board,
                )
                == "t_ambiguous"
            )
        finally:
            conn.close()

    result = ec.manage_execution("t_ambiguous", "status")
    assert result["ok"] is False
    assert result["error"]["code"] == "ambiguous_execution_id"
    assert result["matches"] == 2


def test_mutation_requires_fresh_expected_state_and_request_id():
    task_id = _create_task()
    no_state = ec.manage_execution(task_id, "cancel", request_id="cancel-1")
    assert no_state["error"]["code"] == "expected_state_required"
    no_request = ec.manage_execution(
        task_id, "cancel", expected_state=_token(task_id)
    )
    assert no_request["error"]["code"] == "invalid_request_id"
    stale = ec.manage_execution(
        task_id,
        "cancel",
        expected_state="xec1_" + "0" * 32,
        request_id="cancel-2",
    )
    assert stale["error"]["code"] == "stale_state_token"
    assert stale["state"] == "ready"


def test_state_tokens_are_bound_to_execution_identity():
    task_a = _create_task(title="A")
    task_b = _create_task(title="B")
    status_a = ec.manage_execution(task_a, "status")
    status_b = ec.manage_execution(task_b, "status")

    assert status_a["state"] == status_b["state"] == "ready"
    assert status_a["state_token"] != status_b["state_token"]
    crossed = ec.manage_execution(
        task_b,
        "cancel",
        expected_state=status_a["state_token"],
        request_id="crossed-token",
    )
    assert crossed["ok"] is False
    assert crossed["error"]["code"] == "stale_state_token"
    assert ec.manage_execution(task_b, "status")["state"] == "ready"


def test_kanban_state_token_rejects_ready_running_ready_aba(monkeypatch):
    task_id = _create_task()
    old_token = _token(task_id)
    _claim_task(task_id)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    conn = kb.connect()
    try:
        assert kb.reclaim_task(conn, task_id, reason="ABA setup") is True
        assert kb.get_task(conn, task_id).status == "ready"
    finally:
        conn.close()

    result = ec.manage_execution(
        task_id,
        "cancel",
        expected_state=old_token,
        request_id="aba-cancel",
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "stale_state_token"
    assert result["state"] == "ready"
    assert result["state_token"] != old_token


def test_async_duplicate_cancel_is_terminally_idempotent():
    execution_id, _gate = _dispatch()
    token = _token(execution_id)
    first = ec.manage_execution(
        execution_id,
        "cancel",
        expected_state=token,
        request_id="same-cancel",
    )
    assert first["ok"] is True
    assert first["idempotent"] is False
    durable = _wait_terminal(execution_id)
    assert durable["state"] == "cancelled"
    with ad._records_lock:
        ad._records.pop(execution_id, None)

    retry = ec.manage_execution(
        execution_id,
        "cancel",
        expected_state=token,
        request_id="same-cancel",
    )
    assert retry["ok"] is True
    assert retry["idempotent"] is True
    assert retry["state"] == "cancelled"
    conflict = ec.manage_execution(
        execution_id,
        "cancel",
        instruction="different payload after restart",
        expected_state=token,
        request_id="same-cancel",
    )
    assert conflict["error"]["code"] == "idempotency_conflict"


def test_async_status_does_not_advertise_cancel_without_live_owner():
    execution_id, gate = _dispatch()
    with ad._records_lock:
        ad._records.pop(execution_id, None)

    status = ec.manage_execution(execution_id, "status")
    assert status["state"] == "running"
    assert status["allowed_actions"] == ["status"]
    refused = ec.manage_execution(
        execution_id,
        "cancel",
        expected_state=status["state_token"],
        request_id="orphan-cancel",
    )
    assert refused["error"]["code"] == "not_live_in_this_process"
    gate.set()


def test_async_request_id_reuse_with_different_instruction_conflicts():
    execution_id, _gate = _dispatch()
    token = _token(execution_id)
    assert ec.manage_execution(
        execution_id,
        "cancel",
        instruction="first",
        expected_state=token,
        request_id="cancel-payload",
    )["ok"] is True
    conflict = ec.manage_execution(
        execution_id,
        "cancel",
        instruction="different",
        expected_state=token,
        request_id="cancel-payload",
    )
    assert conflict["error"]["code"] == "idempotency_conflict"


def test_async_completion_wins_before_late_control():
    execution_id, _gate = _dispatch(
        runner=lambda: {"status": "completed", "summary": "already done"}
    )
    assert _wait_terminal(execution_id)["state"] == "completed"
    result = ec.manage_execution(
        execution_id,
        "cancel",
        expected_state=_token(execution_id),
        request_id="late-cancel",
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "not_running"
    assert result["state"] == "completed"


def test_async_signal_failure_is_stable_and_does_not_relabel_completion():
    gate = threading.Event()

    def runner():
        gate.wait(timeout=5)
        return {"status": "completed", "summary": "late success"}

    class RejectedChild:
        def interrupt(self, _reason):
            raise RuntimeError("worker channel closed")

    def fail_signal():
        _interrupt_async_children([RejectedChild(), object()])

    execution_id, _ = _dispatch(interrupt_fn=fail_signal, runner=runner)
    token = _token(execution_id)
    result = ec.manage_execution(
        execution_id,
        "cancel",
        expected_state=token,
        request_id="signal-failure",
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "signal_failed"
    assert result["action_committed"] is False
    assert result["signal_applied"] is False
    retry = ec.manage_execution(
        execution_id,
        "cancel",
        expected_state=token,
        request_id="signal-failure",
    )
    assert retry["ok"] is False
    assert retry["error"]["code"] == "signal_failed"
    assert retry["action_committed"] is False
    gate.set()
    assert _wait_terminal(execution_id)["state"] == "completed"


def test_async_partial_batch_signal_failure_does_not_commit_cancel():
    gate = threading.Event()
    accepted = []

    class AcceptedChild:
        def interrupt(self, reason):
            accepted.append(reason)

    class RejectedChild:
        def interrupt(self, _reason):
            raise RuntimeError("second worker rejected")

    def runner():
        gate.wait(timeout=5)
        return {"status": "completed", "summary": "batch completed"}

    execution_id, _ = _dispatch(
        interrupt_fn=lambda: _interrupt_async_children(
            [AcceptedChild(), RejectedChild()]
        ),
        runner=runner,
    )
    token = _token(execution_id)
    result = ec.manage_execution(
        execution_id,
        "cancel",
        expected_state=token,
        request_id="partial-signal-failure",
    )
    assert result["error"]["code"] == "signal_failed"
    assert result["action_committed"] is False
    assert accepted == ["Async delegation cancelled"]
    gate.set()
    assert _wait_terminal(execution_id)["state"] == "completed"


def test_kanban_interrupt_is_resumable_and_duplicate_is_idempotent(monkeypatch):
    task_id = _create_task()
    _claim_task(task_id)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    signals = []
    token = _token(task_id)

    first = ec.manage_execution(
        task_id,
        "interrupt",
        instruction="change direction",
        expected_state=token,
        request_id="interrupt-1",
        signal_fn=lambda pid, sig: signals.append((pid, sig)),
    )
    assert first["ok"] is True
    assert first["state"] == "ready"
    assert len(signals) == 1

    retry = ec.manage_execution(
        task_id,
        "interrupt",
        instruction="change direction",
        expected_state=token,
        request_id="interrupt-1",
        signal_fn=lambda *_args: pytest.fail("duplicate re-signalled worker"),
    )
    assert retry["ok"] is True
    assert retry["idempotent"] is True
    assert retry["state"] == "ready"


def test_kanban_interrupt_tolerates_benign_event_after_worker_signal(
    monkeypatch,
):
    task_id = _create_task()
    _claim_task(task_id)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

    def signal_with_event(_pid, _sig):
        event_conn = kb.connect()
        try:
            kb._append_event(
                event_conn,
                task_id,
                "heartbeat",
                {"detail": "benign post-signal event"},
            )
            event_conn.commit()
        finally:
            event_conn.close()

    result = ec.manage_execution(
        task_id,
        "interrupt",
        instruction="change direction",
        expected_state=_token(task_id),
        request_id="interrupt-with-benign-event",
        signal_fn=signal_with_event,
    )
    assert result["ok"] is True
    assert result["state"] == "ready"


def test_kanban_cancel_is_terminal_and_idempotent():
    task_id = _create_task()
    token = _token(task_id)
    first = ec.manage_execution(
        task_id,
        "cancel",
        expected_state=token,
        request_id="cancel-task-1",
    )
    assert first["ok"] is True
    assert first["state"] == "archived"

    retry = ec.manage_execution(
        task_id,
        "cancel",
        expected_state=token,
        request_id="cancel-task-1",
    )
    assert retry["ok"] is True
    assert retry["idempotent"] is True
    assert retry["state"] == "archived"


def test_kanban_request_id_reuse_with_different_instruction_conflicts():
    task_id = _create_task()
    token = _token(task_id)
    assert ec.manage_execution(
        task_id,
        "cancel",
        instruction="first",
        expected_state=token,
        request_id="task-payload",
    )["ok"] is True
    conflict = ec.manage_execution(
        task_id,
        "cancel",
        instruction="different",
        expected_state=token,
        request_id="task-payload",
    )
    assert conflict["error"]["code"] == "idempotency_conflict"


def test_kanban_completion_wins_control_race(monkeypatch):
    task_id = _create_task()
    _claim_task(task_id)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    token = _token(task_id)

    def complete_while_signalling(_pid, _sig):
        conn = kb.connect()
        try:
            assert kb.complete_task(conn, task_id, result="finished") is True
        finally:
            conn.close()

    result = ec.manage_execution(
        task_id,
        "cancel",
        expected_state=token,
        request_id="racy-cancel",
        signal_fn=complete_while_signalling,
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "state_changed"
    assert result["state"] == "done"


def test_kanban_worker_signal_failure_leaves_state_unchanged(monkeypatch):
    task_id = _create_task()
    _claim_task(task_id)
    token = _token(task_id)
    monkeypatch.setattr(
        kb,
        "_terminate_reclaimed_worker",
        lambda *_args, **_kwargs: {
            "prev_pid": 99123,
            "host_local": True,
            "termination_attempted": True,
            "terminated": False,
            "sigkill": True,
        },
    )
    result = ec.manage_execution(
        task_id,
        "interrupt",
        expected_state=token,
        request_id="failed-interrupt",
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "worker_signal_failed"
    assert result["state"] == "running"


def test_invalid_request_fields_fail_before_resolution():
    assert ec.manage_execution(
        "t_missing",
        "cancel",
        expected_state="BAD STATE",
        request_id="r",
    )["error"]["code"] == "invalid_expected_state"
    assert ec.manage_execution(
        "t_missing",
        "cancel",
        instruction="x" * 4001,
        expected_state="xec1_" + "0" * 32,
        request_id="r",
    )["error"]["code"] == "invalid_instruction"
