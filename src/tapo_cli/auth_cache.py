"""pytapo session-state cache (SRD §6.4-6.5, FR-CRED-9..13).

Persists pytapo's opaque session state per MAC so subsequent invocations can
skip the handshake. The on-disk schema is intentionally **opaque** — we do
not parse or interpret pytapo's blob. We only attach a top-level
``pytapo_version`` field for invalidation across library upgrades and an
``expires_at`` RFC 3339 string when the caller knows one.

Per-FR contracts honored here:

- **FR-CRED-9** Cache files at ``~/.config/tapo-cli/.tokens/<device-mac>.json``
  with chmod 0600. The directory is created with chmod 0700 on first use.
  Top-level ``pytapo_version`` field; mismatch with currently-installed
  pytapo invalidates the entry and emits one INFO line.
- **FR-CRED-10** ``load`` returns the opaque state blob for the wrapper to
  hand to pytapo.
- **FR-CRED-11** ``flush_one`` lets the auth-retry path invalidate exactly
  one device's cache.
- **FR-CRED-12** ``flush_all`` / ``flush_one`` back the ``auth flush`` verb.
- **FR-CRED-13** Per-device advisory lock via ``flock``. Reads do NOT take
  the lock; only writes and the auth-renew path do. Atomic writes via
  tmpfile + ``fsync`` + rename. Lock timeout = ``--timeout`` (default 5);
  timeout exits 3 (network/contention) NOT 2 (auth), with a structured
  error naming the holding PID when obtainable.
- **FR-CRED-14** ``list_sessions`` exposes per-cache metadata for
  ``auth status``.

Test override: ``TAPO_CLI_CONFIG_DIR`` redirects the cache root to a tmp
path. Documented hatch — :func:`cache_dir` honors it before falling back
to ``~/.config/tapo-cli``.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import errno
import fcntl
import importlib.metadata
import json
import logging
import os
import platform
import stat
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

from tapo_cli.errors import AuthError, ConfigError, NetworkError

logger = logging.getLogger("tapo_cli")


ENV_CONFIG_DIR: Final[str] = "TAPO_CLI_CONFIG_DIR"

CONFIG_DIR_DEFAULT: Final[Path] = Path("~/.config/tapo-cli").expanduser()
TOKENS_SUBDIR: Final[str] = ".tokens"

DIR_MODE: Final[int] = 0o700
FILE_MODE: Final[int] = 0o600

# On-disk schema keys (top-level reserved by tapo-cli; pytapo state nests under "state")
KEY_PYTAPO_VERSION: Final[str] = "pytapo_version"
KEY_STATE: Final[str] = "state"
KEY_EXPIRES_AT: Final[str] = "expires_at"
KEY_SOURCE: Final[str] = "credential_source"
"""Records the credential source that produced the auth (``camera_account``
or ``cloud_account``). FR-CRED-11: cache invalidates when
``--credential-source`` selects a different family than the cache origin."""


# Allowed counter for consecutive auth failures (FR-CRED-11).
MAX_AUTH_RETRIES: Final[int] = 1


# In-process advisory mutex registry — flock against the same file from the
# same process is a no-op on some platforms; this makes it deterministic.
_PROCESS_LOCK_REGISTRY: dict[Path, threading.Lock] = {}
_REGISTRY_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _config_dir() -> Path:
    """Honor ``TAPO_CLI_CONFIG_DIR`` for tests; fall back to ``~/.config/tapo-cli``."""
    override = os.environ.get(ENV_CONFIG_DIR)
    if override:
        return Path(override).expanduser()
    return Path("~/.config/tapo-cli").expanduser()


def cache_dir() -> Path:
    """Return the ``.tokens/`` directory, creating it with chmod 0700 if needed.

    Idempotent — re-tightens the mode if the directory already exists with
    permissive bits.
    """
    base = _config_dir()
    tokens = base / TOKENS_SUBDIR

    base.mkdir(parents=True, exist_ok=True)
    tokens.mkdir(parents=True, exist_ok=True)

    try:
        os.chmod(tokens, DIR_MODE)
    except OSError as exc:
        # Some FS (e.g., FUSE mounts) ignore chmod — keep going; file-level
        # chmod still narrows access.
        logger.debug("could not chmod %s to 0700: %s", tokens, exc)

    return tokens


def cache_path_for_mac(mac: str) -> Path:
    """Return the canonical cache file path for a MAC.

    Normalizes to uppercase colon-form so casing variations don't produce
    duplicate cache entries.
    """
    return cache_dir() / f"{_normalize_mac(mac)}.json"


def lock_path_for_mac(mac: str) -> Path:
    """Sibling lockfile path used by :func:`lock_for_write`."""
    return cache_dir() / f"{_normalize_mac(mac)}.lock"


# ---------------------------------------------------------------------------
# Pytapo version
# ---------------------------------------------------------------------------


def installed_pytapo_version() -> str:
    """Return the version string of the currently-installed pytapo.

    Pytapo (at our pinned SHA) doesn't ship a ``__version__`` attribute, so
    we use ``importlib.metadata`` against the dist's metadata. Falls back to
    ``"unknown"`` if metadata is unavailable (test envs without pytapo
    installed).
    """
    try:
        return importlib.metadata.version("pytapo")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


def save_session(
    mac: str,
    state: dict[str, Any],
    *,
    expires_at: str | None = None,
    credential_source: str | None = None,
    pytapo_version: str | None = None,
) -> None:
    """Persist ``state`` (opaque pytapo blob) atomically.

    Writes to a sibling tempfile, fsyncs, then renames into place. The final
    file gets ``0o600``. Caller MUST hold :func:`lock_for_write` for the same
    MAC for the duration of the surrounding read-modify-write transaction
    (FR-CRED-13).

    Args:
        mac: Target MAC. Normalized to uppercase colon form.
        state: Opaque pytapo session state. Stored verbatim under ``state``.
        expires_at: RFC 3339 UTC string with ``Z`` suffix when the caller
            knows the session lifetime; otherwise ``None`` and ``auth status``
            reports null. SRD §10.5 / §7.2.
        credential_source: ``"camera_account"`` or ``"cloud_account"``;
            recorded for FR-CRED-11 invalidation when ``--credential-source``
            selects a different family.
        pytapo_version: Override for tests. Defaults to the installed
            pytapo version (FR-CRED-9).
    """
    target = cache_path_for_mac(mac)
    target.parent.mkdir(parents=True, exist_ok=True)

    on_disk: dict[str, Any] = {
        KEY_PYTAPO_VERSION: pytapo_version or installed_pytapo_version(),
        KEY_STATE: dict(state),
    }
    if expires_at is not None:
        on_disk[KEY_EXPIRES_AT] = expires_at
    if credential_source is not None:
        on_disk[KEY_SOURCE] = credential_source

    tmp_fd, tmp_path_str = _make_tempfile(target)
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(on_disk, fh, separators=(",", ":"), sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, FILE_MODE)
        os.replace(tmp_path, target)
    except BaseException:
        # Best-effort cleanup of the tempfile if anything went wrong before rename.
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def load_session(
    mac: str,
    *,
    credential_source: str | None = None,
    pytapo_version: str | None = None,
) -> dict[str, Any] | None:
    """Return the opaque pytapo state blob, or ``None`` for absent / invalid.

    Returns ``None`` (and removes the file) when:

    * file doesn't exist
    * file is unparseable JSON
    * ``pytapo_version`` mismatches (FR-CRED-9 — emits one INFO line)
    * ``credential_source`` was supplied and differs from the cached origin
      (FR-CRED-11)

    Reads do NOT take the per-device lock.
    """
    path = cache_path_for_mac(mac)
    if not path.exists():
        return None

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("auth_cache: read failed for %s: %s", path, exc)
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("auth_cache: dropping malformed cache file %s: %s", path, exc)
        with contextlib.suppress(OSError):
            path.unlink()
        return None

    if not isinstance(payload, dict):
        logger.warning("auth_cache: dropping non-object cache file %s", path)
        with contextlib.suppress(OSError):
            path.unlink()
        return None

    cached_version = payload.get(KEY_PYTAPO_VERSION)
    current = pytapo_version or installed_pytapo_version()
    if cached_version != current:
        logger.info(
            "auth_cache: invalidating %s (pytapo_version mismatch: cached=%r installed=%r)",
            path,
            cached_version,
            current,
        )
        with contextlib.suppress(OSError):
            path.unlink()
        return None

    if credential_source is not None:
        cached_source = payload.get(KEY_SOURCE)
        if cached_source != credential_source:
            logger.info(
                "auth_cache: invalidating %s (credential_source mismatch: "
                "cached=%r requested=%r)",
                path,
                cached_source,
                credential_source,
            )
            with contextlib.suppress(OSError):
                path.unlink()
            return None

    state = payload.get(KEY_STATE)
    if not isinstance(state, dict):
        logger.warning("auth_cache: dropping cache file %s with non-object state", path)
        with contextlib.suppress(OSError):
            path.unlink()
        return None

    return state


# ---------------------------------------------------------------------------
# Flush
# ---------------------------------------------------------------------------


def flush_all() -> int:
    """Delete every cached session. Returns count of files removed."""
    base = cache_dir()
    removed = 0
    for entry in base.iterdir():
        if entry.is_file() and entry.suffix == ".json":
            try:
                entry.unlink()
                removed += 1
            except OSError as exc:
                logger.warning("auth_cache: could not unlink %s: %s", entry, exc)
    return removed


def flush_one(mac: str) -> bool:
    """Delete exactly one device's cached session. Returns True on hit."""
    path = cache_path_for_mac(mac)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("auth_cache: could not unlink %s: %s", path, exc)
        return False


# ---------------------------------------------------------------------------
# List (auth status, FR-CRED-14)
# ---------------------------------------------------------------------------


def list_session_files() -> list[Path]:
    """Enumerate every cache file path. Caller decorates with alias/account info."""
    base = cache_dir()
    out: list[Path] = []
    for entry in sorted(base.iterdir()):
        if entry.is_file() and entry.suffix == ".json":
            out.append(entry)
    return out


def read_session_meta(path: Path) -> dict[str, Any]:
    """Read on-disk metadata fields (no state). Best-effort — never raises."""
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("auth_cache: could not parse %s: %s", path, exc)
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        KEY_PYTAPO_VERSION: payload.get(KEY_PYTAPO_VERSION, "unknown"),
        KEY_EXPIRES_AT: payload.get(KEY_EXPIRES_AT),
        KEY_SOURCE: payload.get(KEY_SOURCE),
    }


def mtime_rfc3339(path: Path) -> str:
    """Return the cache file's mtime as an RFC 3339 UTC string ('Z' suffix)."""
    epoch = path.stat().st_mtime
    return (
        dt.datetime.fromtimestamp(epoch, tz=dt.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# Per-device lock (FR-CRED-13)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def lock_for_write(mac: str, timeout: float) -> Iterator[None]:
    """Acquire an advisory write lock for one device.

    Implemented with ``fcntl.flock`` on a sibling lockfile so we don't have
    to truncate the actual cache file just to lock it. A polling loop
    enforces the ``timeout`` (``flock`` itself has no portable timeout).

    A second concurrent invocation that fails to acquire within ``timeout``
    raises :class:`NetworkError` with exit code 3 per FR-CRED-13. The error's
    ``extra.holder_pid`` carries the lock-holder's PID when obtainable
    (Linux: ``/proc/locks``; macOS: best-effort via ``lsof``; omitted otherwise).

    Reads do NOT take this lock — only writes and the auth-renew path do.
    """
    if timeout < 0:
        raise ConfigError(
            f"lock_for_write timeout must be >= 0, got {timeout}",
        )

    lock_path = lock_path_for_mac(mac)

    # Per-process registry mutex first so two threads in the same process
    # serialize cleanly on platforms where flock against the same FD in the
    # same process is a no-op.
    proc_lock = _get_proc_lock(lock_path)
    proc_lock_acquired = proc_lock.acquire(timeout=timeout if timeout > 0 else 0.0001)
    if not proc_lock_acquired:
        raise NetworkError(
            f"timed out waiting for cache lock on {mac} after {timeout:.2f}s",
            hint="Another tapo-cli invocation is mid-handshake. Retry shortly.",
            target=mac,
            extra={"mac": mac, "timeout_seconds": timeout},
        )

    fd: int | None = None
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        with contextlib.suppress(OSError):
            os.chmod(lock_path, FILE_MODE)

        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    holder = _holder_pid(lock_path)
                    extra: dict[str, Any] = {"mac": mac, "timeout_seconds": timeout}
                    if holder is not None:
                        extra["holder_pid"] = holder
                    raise NetworkError(
                        f"timed out waiting for cache lock on {mac} "
                        f"after {timeout:.2f}s",
                        hint="Another tapo-cli invocation is mid-handshake. Retry shortly.",
                        target=mac,
                        extra=extra,
                    ) from exc
                time.sleep(0.05)

        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        proc_lock.release()


def _holder_pid(lock_path: Path) -> int | None:
    """Best-effort: return the PID currently holding the flock, or ``None``."""
    if platform.system() == "Linux":
        try:
            with Path("/proc/locks").open(encoding="utf-8") as fh:
                inode = lock_path.stat().st_ino
                # /proc/locks lines look like:
                # "1: FLOCK ADVISORY WRITE 12345 fd:01:1234567 0 EOF"
                for line in fh:
                    parts = line.split()
                    if len(parts) >= 6 and ":" in parts[5]:
                        line_inode_str = parts[5].split(":")[-1]
                        try:
                            line_inode = int(line_inode_str)
                        except ValueError:
                            continue
                        if line_inode == inode:
                            with contextlib.suppress(ValueError):
                                return int(parts[4])
        except OSError:
            return None
        return None
    # macOS / others: lsof is too heavy and unreliable; skip per FR-CRED-13.
    return None


# ---------------------------------------------------------------------------
# Auth retry counter (FR-CRED-11)
# ---------------------------------------------------------------------------


class AuthRetryExhaustedError(AuthError):
    """Raised by the wrapper layer after two consecutive auth failures.

    Exit 2 per FR-CRED-11. Lives here so the cache-invalidation path and
    the retry counter can share a single exception type.
    """


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _normalize_mac(mac: str) -> str:
    """Uppercase colon-form. Accepts ``aa-bb-cc-dd-ee-ff`` or ``aabbccddeeff``."""
    cleaned = mac.replace("-", "").replace(":", "").strip().upper()
    if len(cleaned) != 12:
        return cleaned
    return ":".join(cleaned[i : i + 2] for i in range(0, 12, 2))


def _make_tempfile(target: Path) -> tuple[int, str]:
    """Create a same-directory tempfile for atomic rename. Returns (fd, path)."""
    import tempfile

    fd, path = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    with contextlib.suppress(OSError):
        os.fchmod(fd, FILE_MODE)
    return fd, path


def _get_proc_lock(path: Path) -> threading.Lock:
    """Return a process-local mutex unique to this lockfile path."""
    with _REGISTRY_LOCK:
        if path not in _PROCESS_LOCK_REGISTRY:
            _PROCESS_LOCK_REGISTRY[path] = threading.Lock()
        return _PROCESS_LOCK_REGISTRY[path]


def _current_dir_mode(path: Path) -> int:
    """For tests — return the current permission bits on a directory."""
    return stat.S_IMODE(path.stat().st_mode)


__all__ = [
    "DIR_MODE",
    "ENV_CONFIG_DIR",
    "FILE_MODE",
    "KEY_EXPIRES_AT",
    "KEY_PYTAPO_VERSION",
    "KEY_SOURCE",
    "KEY_STATE",
    "MAX_AUTH_RETRIES",
    "TOKENS_SUBDIR",
    "AuthRetryExhaustedError",
    "cache_dir",
    "cache_path_for_mac",
    "flush_all",
    "flush_one",
    "installed_pytapo_version",
    "list_session_files",
    "load_session",
    "lock_for_write",
    "lock_path_for_mac",
    "mtime_rfc3339",
    "read_session_meta",
    "save_session",
]
