"""Harso image-owned cron scripts; tenant script execution fails closed."""
from __future__ import annotations
import hashlib
import logging
import os
import re
import stat as _stat
import sys
import errno
from pathlib import Path
from typing import Optional
logger = logging.getLogger("cron.scheduler")

PLATFORM_SCRIPT_PREFIX = "platform:"
_PLATFORM_SCRIPT_NAME_RE = re.compile(r"[a-z0-9_]{1,64}\.py")
# Owner the platform script root and its files must have. Tests patch this
# where uid 0 is unavailable; production never changes it.
_PLATFORM_SCRIPT_OWNER_UID = 0
# Runs the verified platform script from the fd the resolver opened, so the
# executed bytes come from the same object that was checked (never a second
# path lookup). ``python -I`` ignores PYTHON* env, user site and script dir.
_PLATFORM_SCRIPT_BOOTSTRAP = (
    "import os, sys\n"
    "fd, path = int(sys.argv[1]), sys.argv[2]\n"
    "with os.fdopen(fd, 'rb') as f:\n"
    "    src = f.read()\n"
    "sys.argv = [path]\n"
    "exec(compile(src, path, 'exec'),"
    " {'__name__': '__main__', '__file__': path, '__builtins__': __builtins__})\n"
)


def _cron_script_policy() -> Optional[tuple[bool, str, dict]]:
    """Return (allow_scripts, platform_script_root, platform_script_digests).

    Stock defaults: tenant scripts allowed, no platform root, no digests.
    Returns None (callers refuse every script) unless the policy is positively
    valid. Read each layer directly once: upstream config readers recover
    invalid roots/managed policy to defaults. Every raw layer and the merged result
    must also carry the declared types. An explicit null counts as invalid,
    because the merge would otherwise drop it back to the default.
    """
    try:
        from hermes_cli.config import get_config_path
        from hermes_cli.managed_scope import get_managed_dir
        managed_dir = get_managed_dir()
        from utils import fast_safe_load
        layers = []
        for path in (get_config_path(), (managed_dir / "config.yaml" if managed_dir else None)):
            if path is None:
                layers.append({})
                continue
            try:
                with open(path, encoding="utf-8-sig") as stream:
                    if os.fstat(stream.fileno()).st_mode & 0o444 == 0:
                        raise PermissionError("config is unreadable")
                    loaded = fast_safe_load(stream)
                    stream.seek(0)
                    text = stream.read()
            except FileNotFoundError:
                loaded, text = {}, ""
            if loaded is None and not any(line.strip() and not line.lstrip().startswith("#") for line in text.splitlines()):
                loaded = {}
            if not isinstance(loaded, dict):
                raise ValueError("cron script config must be a mapping")
            layers.append(loaded)
        cron_cfg = {}
        for layer in layers:
            if "cron" in layer:
                policy = layer["cron"]
                if not isinstance(policy, dict) or not _cron_script_policy_types_ok(policy):
                    raise ValueError("invalid cron script policy")
                cron_cfg.update(policy)
        return (cron_cfg.get("allow_scripts", True), cron_cfg.get("platform_script_root", ""),
                cron_cfg.get("platform_script_digests", {}))
    except Exception:
        logger.warning("cron: config unreadable or invalid; refusing all cron scripts", exc_info=True)
        return None


def _cron_script_policy_types_ok(cron_cfg: dict) -> bool:
    """True when every script-policy key present in ``cron_cfg`` has its type."""
    digests = cron_cfg.get("platform_script_digests", {})
    return (
        isinstance(cron_cfg.get("allow_scripts", True), bool)
        and isinstance(cron_cfg.get("platform_script_root", ""), str)
        and isinstance(digests, dict)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in digests.items())
    )


def _tenant_scripts_allowed() -> bool:
    policy = _cron_script_policy()
    return policy is not None and policy[0]


def _platform_owned(st: os.stat_result) -> bool:
    return st.st_uid == _PLATFORM_SCRIPT_OWNER_UID and not st.st_mode & 0o022


def _trusted_ancestor(st: os.stat_result) -> bool:
    # Only root / the platform owner can rename or replace an entry here; a
    # sticky world-writable dir (e.g. /tmp) cannot rename a trusted child.
    return (
        _stat.S_ISDIR(st.st_mode)
        and st.st_uid in (0, _PLATFORM_SCRIPT_OWNER_UID)
        and (not st.st_mode & 0o022 or bool(st.st_mode & _stat.S_ISVTX))
    )


def _open_platform_script(ref: str) -> tuple[Optional[int], Optional[Path], Optional[str]]:
    """Verify a ``platform:<name>`` ref and return (fd, path, None) or (None, None, "Blocked: ...").

    The root must be a canonical absolute path (no symlink, ``.``, ``..`` or
    duplicate/trailing slash), every ancestor must be trusted-owned and not
    group/other-writable (sticky allowed), and the root and file must be
    owner-``_PLATFORM_SCRIPT_OWNER_UID`` and not group/other-writable. The file
    is opened once with O_NOFOLLOW and checked with fstat; when
    ``cron.platform_script_digests`` is non-empty the sha256 of the bytes read
    from that fd must be pinned under its name. The returned fd is at offset 0;
    the caller owns it. Every I/O failure is a Blocked result.
    """
    name = str(ref)[len(PLATFORM_SCRIPT_PREFIX):]
    if not _PLATFORM_SCRIPT_NAME_RE.fullmatch(name):
        return None, None, f"Blocked: invalid platform script name: {ref!r}"
    if sys.platform == "win32":
        return None, None, f"Blocked: platform scripts require POSIX ownership: {ref!r}"
    policy = _cron_script_policy()
    if policy is None:
        return None, None, f"Blocked: cron script policy is invalid or unreadable: {ref!r}"
    _, root, digests = policy
    if not root:
        return None, None, f"Blocked: cron.platform_script_root is not configured: {ref!r}"
    fd = None
    try:
        if not os.path.isabs(root) or os.path.realpath(root) != root:
            return None, None, f"Blocked: platform script root is not a canonical absolute path: {root!r}"
        if not _platform_owned(os.lstat(root)):
            return None, None, f"Blocked: platform script root is not platform-owned: {root}"
        parent = os.path.dirname(root)
        while True:
            if not _trusted_ancestor(os.lstat(parent)):
                return None, None, f"Blocked: platform script root has an untrusted ancestor: {parent}"
            if parent == os.path.dirname(parent):
                break
            parent = os.path.dirname(parent)
        path = Path(root) / name
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                return None, None, f"Blocked: platform script is not a regular file: {ref!r}"
            raise
        st = os.fstat(fd)
        if st.st_mode & 0o444 == 0:
            return None, None, f"Blocked: platform script cannot be read: {ref!r}"
        if not _stat.S_ISREG(st.st_mode):
            return None, None, f"Blocked: platform script is not a regular file: {ref!r}"
        if not _platform_owned(st):
            return None, None, f"Blocked: platform script is not platform-owned: {ref!r}"
        if digests:
            with os.fdopen(os.dup(fd), "rb") as fh:  # windows-footgun: ok (binary)
                actual = hashlib.sha256(fh.read()).hexdigest()
            os.lseek(fd, 0, os.SEEK_SET)
            pinned = digests.get(name)
            if pinned is None or pinned.strip().lower() != actual:
                return None, None, f"Blocked: platform script digest is not pinned: {ref!r}"
        verified, fd = fd, None
        return verified, path, None
    except (OSError, ValueError) as exc:
        return None, None, f"Blocked: platform script cannot be read: {ref!r} ({exc})"
    finally:
        if fd is not None:
            os.close(fd)


def resolve_platform_script(ref: str) -> tuple[Optional[Path], Optional[str]]:
    """Verify a ``platform:<name>`` ref; return (path, None) or (None, "Blocked: ...")."""
    fd, path, error = _open_platform_script(ref)
    if fd is not None:
        os.close(fd)
    return path, error

