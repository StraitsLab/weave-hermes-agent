"""Memory-only credentials for a single live Hermes session."""

from __future__ import annotations

import re
import threading
from datetime import datetime, timezone


class SessionCredential:
    """One redacted, revocable callable bearer for a live session only."""

    def __init__(self, bearer: str, expires_at: datetime) -> None:
        self._lock = threading.Lock()
        self._bearer = bearer
        self._expires_at = expires_at
        self._revoked = False

    def __call__(self) -> str:
        with self._lock:
            if self._revoked or self._expires_at <= datetime.now(timezone.utc):
                raise RuntimeError("credential unavailable")
            return self._bearer

    def refresh(self, bearer: str, expires_at: datetime) -> bool:
        with self._lock:
            if self._revoked or self._expires_at <= datetime.now(timezone.utc):
                return False
            self._bearer = bearer
            self._expires_at = expires_at
            return True

    def revoke(self) -> None:
        with self._lock:
            self._revoked = True
            self._bearer = ""

    def __repr__(self) -> str:
        return "<SessionCredential redacted>"


_CREDENTIAL_EXPIRY_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def parse_credential_expiry(value: object) -> datetime | None:
    if not isinstance(value, str) or not _CREDENTIAL_EXPIRY_RE.fullmatch(value):
        return None
    try:
        expires_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return expires_at if expires_at > datetime.now(timezone.utc) else None
