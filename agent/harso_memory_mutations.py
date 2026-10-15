"""Request-owned native memory mutation coordination."""
import threading
from typing import Any, Callable, Dict


class NativeMemoryMutations:
    def commit_native_mutation(
        self,
        operation_id: str,
        action: str,
        target: str,
        content: str,
        native_writer: Callable[[], dict[str, Any]],
        *,
        old_text: str = "",
    ) -> dict[str, Any]:
        """Commit one native mutation, then mirror it outside coordination.

        ``operation_id`` belongs to the caller and is the sole retry key;
        content is intentionally never used as identity. Followers wait for
        an in-flight operation and receive its exact native result.
        """
        if not operation_id:
            raise ValueError("operation_id is required")
        with self._native_lock:
            cached = self._native_completed.get(operation_id)
            if cached is not None:
                return dict(cached)
            waiter = self._native_inflight.get(operation_id)
            if waiter is None:
                waiter = threading.Event()
                self._native_inflight[operation_id] = waiter
                self._native_owners[operation_id] = threading.get_ident()
                owner = True
            else:
                owner = False
        if not owner:
            if self._native_owners.get(operation_id) == threading.get_ident():
                return {"success": False, "operation_id": operation_id,
                        "error": "native_operation_reentrant"}
            waiter.wait()
            with self._native_lock:
                return dict(self._native_completed.get(operation_id) or {
                    "success": False, "operation_id": operation_id,
                    "error": "native_operation_in_doubt",
                })

        try:
            if self._native_journal is None:
                from agent.native_mutation_journal import NativeMutationJournal
                self._native_journal = NativeMutationJournal()
            recorded = self._native_journal.begin(operation_id)
            if recorded is not None:
                with self._native_lock:
                    self._native_completed[operation_id] = dict(recorded)
                return dict(recorded)
            result = dict(native_writer())
            if result.get("success") is True:
                result["operation_id"] = operation_id
                self._mirror_native_mutation(result, operation_id, action, target, content, old_text)
            if self._native_journal is not None:
                result = self._native_journal.complete(operation_id, result)
            with self._native_lock:
                self._native_completed[operation_id] = dict(result)
            return result
        except RuntimeError as exc:
            if str(exc) == "native_operation_in_doubt":
                result = {"success": False, "operation_id": operation_id, "error": str(exc)}
            else:
                result = {"success": False, "operation_id": operation_id,
                          "error": "native_mutation_failed"}
                if self._native_journal is not None:
                    self._native_journal.fail(operation_id, str(exc))
            with self._native_lock:
                self._native_completed[operation_id] = result
            return result
        except Exception as exc:  # health: allow BLE001 -- every writer failure must close the journal entry so a retry is not stuck in doubt
            result = {"success": False, "operation_id": operation_id,
                      "error": "native_mutation_failed"}
            try:
                if self._native_journal is not None:
                    self._native_journal.fail(operation_id, str(exc))
            finally:
                with self._native_lock:
                    self._native_completed[operation_id] = result
            return result
        finally:
            with self._native_lock:
                event = self._native_inflight.pop(operation_id, None)
                self._native_owners.pop(operation_id, None)
                if event is not None:
                    event.set()

    def _mirror_native_mutation(self, result: dict[str, Any], operation_id: str, action: str, target: str,
                                content: str, old_text: str) -> None:
        """Mirror a committed native write to the external provider; record the outcome on ``result``."""
        result.setdefault("provider_acknowledged", True)
        metadata = {"operation_id": operation_id}
        if "revision" in result:
            metadata["revision"] = result["revision"]
        if old_text:
            metadata["old_text"] = old_text
        if not any(p.name != "builtin" for p in self._providers):
            result["provider_acknowledged"] = False
            result["provider_status"] = "not_configured"
            return
        try:
            acknowledged = self.on_memory_write(action, target, content, metadata=metadata)
        except Exception:  # health: allow BLE001 -- the native write already committed; a mirror failure is reported, not raised
            acknowledged = False
        result["provider_acknowledged"] = bool(acknowledged) and result["provider_acknowledged"]
        result["provider_status"] = "acknowledged" if acknowledged else "failed"
