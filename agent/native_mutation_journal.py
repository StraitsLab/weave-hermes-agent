from __future__ import annotations
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

_NAME = "native-operation-journal.json"
_MAX_TERMINAL = 256
_ERROR_CODES = {"native_mutation_failed", "native_operation_in_doubt",
                "native_operation_reentrant"}
_STATUS_CODES = {"ok", "success", "failed", "error", "conflict", "not_found"}
def compact_native_result(result: Any, *, operation_id: Optional[str] = None) -> Dict[str, Any]:
    """Reduce a terminal result to content-free coordination metadata."""
    source = result if isinstance(result, dict) else {}
    compact: Dict[str, Any] = {}
    for key in ("success", "provider_acknowledged"):
        if isinstance(source.get(key), bool): compact[key] = source[key]
    for key in ("revision", "native_revision"):
        if isinstance(source.get(key), int): compact[key] = source[key]
    for key in ("provider_name",):
        value = source.get(key)
        if isinstance(value, str) and 0 < len(value) <= 64 and all(
                char.isalnum() or char in "._-" for char in value): compact[key] = value
    for key, allowed in (("status", _STATUS_CODES), ("provider_status", {
            "acknowledged", "failed", "not_configured"}), ("error", _ERROR_CODES)):
        if source.get(key) in allowed: compact[key] = source[key]
    identity = operation_id or source.get("operation_id")
    if isinstance(identity, str) and identity: compact["operation_id"] = identity
    if source.get("success") is False and compact.get("error") not in _ERROR_CODES:
        compact["error"] = "native_mutation_failed"
    return compact

class NativeMutationJournal:
    def __init__(self, hermes_home: Optional[Path] = None) -> None:
        if hermes_home is None:
            from hermes_constants import get_hermes_home
            hermes_home = get_hermes_home()
        self.directory = Path(hermes_home) / "memories"
        self.path = self.directory / _NAME

    def _locked(self):
        from tools.memory_tool import MemoryStore
        self.directory.mkdir(parents=True, exist_ok=True)
        return MemoryStore._file_lock(self.path)

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
            if os.name == "posix":
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
                return compact_native_result(existing.get("result"))
            records.append({"operation_id": operation_id, "status": "pending"})
            self._write(records)
        return None

    def _finish(self, operation_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
        with self._locked():
            records = self._read()
            record = self._find(records, operation_id)
            if record is None:
                record = {"operation_id": operation_id}
                records.append(record)
            compact = compact_native_result(result, operation_id=operation_id)
            record.update({"status": "completed", "result": compact})
            terminal = [r for r in records if r.get("status") != "pending"]
            pending = [r for r in records if r.get("status") == "pending"]
            self._write(pending + terminal[-_MAX_TERMINAL:])
        return compact

    def complete(self, operation_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
        return self._finish(operation_id, result)

    def fail(self, operation_id: str, _detail: str) -> None:
        self._finish(operation_id, {"success": False, "operation_id": operation_id,
                                    "error": "native_mutation_failed"})
