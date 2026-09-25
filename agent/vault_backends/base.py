"""Login-backend contract + registry for the browser credential vault.

A ``LoginBackend`` lists login metadata (never secrets) and resolves ONE
password at fill time. External managers (1Password, Bitwarden) additionally
need a per-session unlock; ``resolve_password`` raises ``UnlockRequired``
while locked so the tool can ask the surface to prompt. Handles are
namespaced by ``prefix`` so ``backend_for_handle`` needs no lookup table.
"""

from __future__ import annotations

import logging
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from agent.vault_store import VaultItemMeta

logger = logging.getLogger(__name__)


class UnlockRequired(Exception):
    """The backend is locked for this session; the surface must prompt for the master password."""

    def __init__(self, backend: "LoginBackend"):
        super().__init__(f"{backend.display_name} is locked")
        self.backend = backend


class VaultUseRefused(Exception):
    """Fork (V5): the authority behind a backend declined this one use (approval pending, wrong site, rate
    limit, item changed). ``error_type`` is the tool's typed result; the message is content-free."""

    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type


class VaultUnavailable(RuntimeError):
    """Fork (V5): the backend could not serve the call (unconfigured, unreachable, malformed answer).
    Content-free by contract: never a secret, a bearer or a response body."""


class LoginBackend(ABC):
    name: str                # config key: local | weave | onepassword | bitwarden
    display_name: str        # user-facing
    prefix: str              # handle prefix ("vault_", "wv:", "op:", "bw:")
    needs_unlock: bool = False
    # Fork (V5): can this backend store a login typed into this process? The tool asks the user for a
    # password only when one can, so a backend whose secrets must never pass through the runtime (weave)
    # is never handed one.
    can_save: bool = False
    # Fork (V5): every value this backend resolves is protected (the model never sees it), addresses included.
    # The local vault keeps upstream's rule that an address is not a secret; a Harso item is (VAULT-design §4).
    protects_all_values: bool = False

    def owns(self, handle: str) -> bool:
        return handle.startswith(self.prefix)

    def is_unlocked(self) -> bool:
        return True

    @abstractmethod
    def list_items(self) -> List[VaultItemMeta]:
        """Metadata only. Locked external backends return [] (the agent sees a lock hint instead)."""

    @abstractmethod
    def get_meta(self, handle: str) -> Optional[VaultItemMeta]: ...

    # Fork (V5): every resolve names the exact page origin it fills. A backend whose authority checks the
    # site (weave) refuses without it; the local and manager backends are bound by the tool's own check.
    @abstractmethod
    def resolve_password(self, handle: str, *, origin: Optional[str] = None) -> str:
        """Server-side only; raises ``UnlockRequired`` when locked."""

    def resolve_otp(self, handle: str, *, origin: Optional[str] = None) -> Optional[str]:
        """Current one-time code for a login that stores a TOTP seed, else None (the user is asked).
        Server-side only, like resolve_password."""
        return None

    def resolve_secret(self, handle: str, *, origin: Optional[str] = None) -> Dict[str, str]:
        """Full payload of a payment/address item (server-side only). External managers list only
        logins, so the base returns the password-only shape."""
        return {"password": self.resolve_password(handle, origin=origin)}

    def save_login(self, label: str, origin: str, identifier: str, identifier_type: str,
                   password: str) -> VaultItemMeta:
        """Store one login bound to ``origin`` (only when ``can_save``)."""
        raise NotImplementedError(f"{self.display_name} does not store logins from this session")


def run_with_stdin_secret(argv: Sequence[str], *, env: Dict[str, str], secret: str, timeout: float,
                          label: str) -> subprocess.CompletedProcess:
    """Run a manager CLI feeding *secret* on stdin (never argv, never env). Spawn/timeout → RuntimeError."""
    try:
        return subprocess.run(  # noqa: S603 — argv list, no shell
            list(argv), env=env, input=secret + "\n", capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{label} unlock timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        raise RuntimeError(f"failed to invoke {label}: {exc}") from exc


def run_with_secret_env(argv: Sequence[str], *, env: Dict[str, str], secret_env: str, secret: str, timeout: float,
                        label: str) -> subprocess.CompletedProcess:
    """Run a manager CLI whose non-interactive contract reads the secret from a named env var.
    The variable is set on the child's environment only (never argv, never our process)."""
    child_env = dict(env)
    child_env[secret_env] = secret
    try:
        return subprocess.run(  # noqa: S603 — argv list, no shell
            list(argv), env=child_env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{label} unlock timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        raise RuntimeError(f"failed to invoke {label}: {exc}") from exc


def _cfg() -> Dict:
    from hermes_cli.config import load_config_readonly
    cfg = load_config_readonly().get("vault") or {}
    return cfg if isinstance(cfg, dict) else {}


def external_backend_classes():
    # Fork port (@49b4286a22): the 1Password / Bitwarden backends are NOT ported — cells run the
    # local vault only (V0a). Upstream imports both modules here; with none present, no external
    # manager is ever a login source.
    return ()


def is_installed(name: str) -> bool:
    """Is the manager CLI reachable — honouring a configured ``binary_path`` over PATH."""
    import shutil
    section = _cfg().get(name) or {}
    explicit = str(section.get("binary_path") or "") if isinstance(section, dict) else ""
    if explicit:
        return Path(explicit).is_file()
    if name == "onepassword":
        from agent.secret_sources.onepassword import find_op
        return find_op() is not None
    return shutil.which("bw") is not None


def is_enabled(name: str) -> bool:
    """An installed manager is a login source unless the user opted out (``vault.<name>.enabled: false``).
    Zero-config on purpose: a user with ``bw``/``op`` on PATH should never have to discover a toggle."""
    section = _cfg().get(name) or {}
    if isinstance(section, dict) and section.get("enabled") is False:
        return False
    return is_installed(name)


def enabled_backends() -> List[LoginBackend]:
    """Local first, then every detected external manager the user has not turned off.

    Fork (V5): ``vault.backend: weave`` makes the Harso vault the ONLY login source (a Harso cell or Work
    attempt): no local Fernet file and no manager CLI session may hold a secret there. Any other value than
    ``local``/``weave`` leaves the vault with no source at all rather than guessing one."""
    from agent.vault_backends.local import LocalLoginBackend

    cfg = _cfg()
    choice = cfg.get("backend", "local")
    if choice == "weave":
        from agent.vault_backends.weave import WeaveLoginBackend, timeout_seconds

        # Read per call from the profile's config.yaml (mtime-cached): an edit applies to the next vault call.
        return [WeaveLoginBackend(str(cfg.get("weave_api_url") or ""),
                                  timeout_seconds(cfg.get("weave_timeout_seconds")))]
    if choice != "local":
        logger.warning("vault.backend %.40r is not a known backend; the vault has no login source", choice)
        return []
    out: List[LoginBackend] = [LocalLoginBackend()]
    for cls in external_backend_classes():
        if is_enabled(cls.name):
            section = cfg.get(cls.name) or {}
            out.append(cls(section if isinstance(section, dict) else {}))
    return out


def backend_for_handle(handle: str) -> Optional[LoginBackend]:
    return next((b for b in enabled_backends() if b.owns(handle)), None)
