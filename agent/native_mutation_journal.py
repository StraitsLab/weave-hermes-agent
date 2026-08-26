"""Small, profile-scoped journal for native memory mutation outcomes."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

_NAME = "native-operation-journal.json"
_MAX_TERMINAL = 256


class NativeMutationJournal:
    """Durably coordinate native operation retries for one Hermes home."""

    def __init__(self, hermes_home: Optional[Path] = None) -> None:
        if hermes_home is None:
            from hermes_constants import get_hermes_home
            hermes_home = get_hermes_home()
        self.directory = Path(hermes_home) / "memories"
        self.path = self.directory / _NAME
        self.lock_path = self.directory / (_NAME + ".lock")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        import fcntl
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read(self) -> list[Dict[str, Any]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError("native_operation_in_doubt") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
            raise RuntimeError("native_operation_in_doubt")
        records = payload["records"]
        if not all(isinstance(record, dict) and isinstance(record.get("operation_id"), str)
                   and record.get("status") in {"pending", "completed"} for record in records):
            raise RuntimeError("native_operation_in_doubt")
        return records

    def _write(self, records: list[Dict[str, Any]]) -> None:
        payload = json.dumps({"records": records}, separators=(",", ":"), sort_keys=True)
        fd, temporary = tempfile.mkstemp(prefix=".native-operation-journal-", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            dir_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    @staticmethod
    def _find(records: list[Dict[str, Any]], operation_id: str) -> Optional[Dict[str, Any]]:
        return next((record for record in records if record.get("operation_id") == operation_id), None)

    def begin(self, operation_id: str) -> Optional[Dict[str, Any]]:
        if not operation_id:
            raise ValueError("operation_id is required")
        with self._locked():
            records = self._read()
            existing = self._find(records, operation_id)
            if existing is not None:
                if existing.get("status") == "pending":
                    raise RuntimeError("native_operation_in_doubt")
                return dict(existing.get("result") or {})
            records.append({"operation_id": operation_id, "status": "pending"})
            self._write(records)
        return None

    def _finish(self, operation_id: str, result: Dict[str, Any]) -> None:
        with self._locked():
            records = self._read()
            record = self._find(records, operation_id)
            if record is None:
                record = {"operation_id": operation_id}
                records.append(record)
            record.update({"status": "completed", "result": dict(result)})
            terminal = [r for r in records if r.get("status") != "pending"]
            pending = [r for r in records if r.get("status") == "pending"]
            self._write(pending + terminal[-_MAX_TERMINAL:])

    def complete(self, operation_id: str, result: Dict[str, Any]) -> None:
        self._finish(operation_id, result)

    def fail(self, operation_id: str, detail: str) -> None:
        self._finish(operation_id, {
            "success": False,
            "operation_id": operation_id,
            "error": "native_mutation_failed",
            "detail": str(detail),
        })
