"""Credential resolver for tapo-cli (SRD §6.1-6.7).

Two distinct credential families:

* **Camera account** (per-device, primary in v1.1+) — file referenced from
  ``[devices.<alias>] camera_account_file``. Required for ``stream``/``record``
  (RTSP) and is the preferred control-plane path on current pytapo firmware.
  v1 file format: ``{"version": 1, "username": "<6-32>", "password": "<6-32>"}``.
* **Cloud account** (TP-Link app login, fallback) — shared with ``kasa-cli``
  at ``~/.config/kasa-cli/credentials`` by default per FR-CRED-3.1. A
  tapo-only override at ``~/.config/tapo-cli/credentials`` wins when present.
  ``tapo-cli`` SHALL NEVER write the kasa-cli file.

Resolution order for the *control plane* (FR-CRED-1..3, §6.2):

1. Per-device camera account file (PRIMARY).
2. Per-device cloud-account override (legacy-firmware fallback).
3. Default cloud-account credentials file (with tapo-only override priority).
4. ``TAPO_USERNAME`` + ``TAPO_PASSWORD`` env vars (cloud account).
5. None — RTSP-using verbs exit 2 (FR-CRED-7); control-plane verbs may exit 2
   when no source produced credentials.

The ``--credential-source`` flag (FR-CRED-15, §6.7) constrains which sources
are consulted — see :func:`resolve_control_plane`.

Strict integrity invariants (apply regardless of ``--credential-source``):

* Mode more permissive than 0600 → exit 2 (FR-CRED-2).
* Symlinks refused outright (R5).
* Unknown extra keys → exit 6.
* Username/password length out of [6, 32] → exit 6 for camera-account files.
* Missing ``version`` → treated as v1 with one stderr deprecation warning.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
from pathlib import Path
from typing import Any, Final, Literal

from tapo_cli.config import Config
from tapo_cli.errors import AuthError, ConfigError
from tapo_cli.types import ResolvedCredential

logger = logging.getLogger("tapo_cli")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ENV_USERNAME: Final[str] = "TAPO_USERNAME"
ENV_PASSWORD: Final[str] = "TAPO_PASSWORD"

CURRENT_VERSION: Final[int] = 1
ALLOWED_CRED_KEYS_V1: Final[frozenset[str]] = frozenset({"version", "username", "password"})

# Camera-account length bounds per FR-CRED-5 / Tapo-app UI.
CAMERA_ACCOUNT_MIN: Final[int] = 6
CAMERA_ACCOUNT_MAX: Final[int] = 32

# Tapo-only override path (FR-CRED-3.1). When present, wins over the shared
# kasa-cli file.
TAPO_OVERRIDE_PATH: Final[str] = "~/.config/tapo-cli/credentials"
KASA_SHARED_PATH: Final[str] = "~/.config/kasa-cli/credentials"

CredentialSource = Literal["env", "file", "none"]


# Process-wide latch so the missing-version deprecation warning is emitted at
# most once per process per file path, and the partial-env-fallthrough WARN
# at most once per process.
_DEPRECATION_LOCK = threading.Lock()
_DEPRECATION_WARNED: set[str] = set()
_PARTIAL_ENV_WARNED = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_control_plane(
    config: Config,
    *,
    alias: str | None = None,
    source: CredentialSource | None = None,
) -> ResolvedCredential | None:
    """Walk the §6.2 control-plane resolution chain.

    Args:
        config: Active config.
        alias: Target device alias; gates per-device file lookups.
        source: Override per FR-CRED-15. ``None`` means walk the full chain.

    Returns:
        ResolvedCredential on hit, or ``None`` when no source produced one.
        For control-plane verbs, ``None`` typically maps to exit 2 at the
        verb layer (auth error: missing credentials).
    """
    if source == "none":
        logger.debug("credential-source=none — skipping all sources")
        return None

    # 1) Per-device camera account file (PRIMARY, file source).
    if source != "env" and alias is not None:
        entry = config.devices.get(alias)
        if entry is not None and entry.camera_account_file:
            cred = _load_camera_account_file(
                _expand(entry.camera_account_file),
                source_label=f"per-device-camera:{alias}",
            )
            if cred is not None:
                return cred

    # 2) Per-device cloud-account override.
    if source != "env" and alias is not None:
        entry = config.devices.get(alias)
        if entry is not None and entry.credential_file:
            cred = _load_cloud_account_file(
                _expand(entry.credential_file),
                source_label=f"per-device-cloud:{alias}",
            )
            if cred is not None:
                return cred

    # 3) Default cloud-account file (with FR-CRED-3.1 priority: tapo-only
    #    override wins over the shared kasa-cli file).
    if source != "env":
        cred = _load_default_cloud_file(config)
        if cred is not None:
            return cred

    # 4) Environment variables (cloud account). Partial-set falls through
    #    with a one-shot WARN per §6.2 note.
    if source != "file":
        cred = _from_env()
        if cred is not None:
            return cred

    return None


def resolve_camera_account(
    config: Config,
    *,
    alias: str,
) -> ResolvedCredential:
    """Resolve a camera-account credential for an RTSP-using verb.

    Per §6.2 the resolver SHALL stop at step 1 — only the per-device
    camera_account_file is consulted. No fallback to cloud or env.

    Raises:
        AuthError: when no ``camera_account_file`` is configured for the
            target. FR-CRED-7 — exit 2 with the Tapo-app menu hint.
    """
    entry = config.devices.get(alias)
    if entry is None or not entry.camera_account_file:
        raise AuthError(
            f"no camera_account_file configured for alias {alias!r}",
            target=alias,
            credential="camera_account",
            hint=(
                "Create a camera account in the Tapo app: "
                "Settings > Advanced settings > Camera account, "
                f"then set [devices.{alias}] camera_account_file = '<path>' in config."
            ),
        )
    cred = _load_camera_account_file(
        _expand(entry.camera_account_file),
        source_label=f"per-device-camera:{alias}",
    )
    if cred is None:
        raise AuthError(
            f"camera_account_file missing or unreadable for alias {alias!r}",
            target=alias,
            credential="camera_account",
            hint=f"Check that {entry.camera_account_file} exists and is mode 0600.",
            extra={"path": str(_expand(entry.camera_account_file))},
        )
    return cred


def default_cloud_file_path(config: Config) -> Path:
    """Return the resolved default cloud-account file path.

    Honors FR-CRED-3.1: ``~/.config/tapo-cli/credentials`` wins over the
    configured ``[credentials] file_path`` (which defaults to the shared
    kasa-cli file) when the tapo-only override exists.
    """
    override = _expand(TAPO_OVERRIDE_PATH)
    if override.exists():
        return override
    return _expand(config.credentials.file_path)


# ---------------------------------------------------------------------------
# File loaders
# ---------------------------------------------------------------------------


def _load_default_cloud_file(config: Config) -> ResolvedCredential | None:
    """Resolve the default cloud-account file per FR-CRED-3.1.

    Tapo-only override at ``~/.config/tapo-cli/credentials`` wins when it
    exists; otherwise falls through to the configured ``file_path`` (which
    defaults to the shared kasa-cli file).
    """
    override = _expand(TAPO_OVERRIDE_PATH)
    if override.exists():
        return _load_cloud_account_file(override, source_label=str(override))

    default_path = _expand(config.credentials.file_path)
    return _load_cloud_account_file(default_path, source_label=str(default_path))


def _load_cloud_account_file(path: Path, *, source_label: str) -> ResolvedCredential | None:
    """Load a cloud-account JSON v1 file. Returns ``None`` when absent."""
    if not path.exists():
        logger.debug("cloud-account credentials file not present: %s", path)
        return None

    payload = _read_json_v1(path)
    username = _require_str(payload, "username", path)
    password = _require_str(payload, "password", path)

    return ResolvedCredential(
        username=username,
        password=password,
        family="cloud_account",
        source=source_label,
    )


def _load_camera_account_file(path: Path, *, source_label: str) -> ResolvedCredential | None:
    """Load a camera-account JSON v1 file with length validation.

    Returns ``None`` only when the file is absent — schema/permission
    failures still raise (they are integrity errors, not fall-through cases).
    """
    if not path.exists():
        logger.debug("camera-account file not present: %s", path)
        return None

    payload = _read_json_v1(path)
    username = _require_str(payload, "username", path)
    password = _require_str(payload, "password", path)

    for field_name, value in (("username", username), ("password", password)):
        if not (CAMERA_ACCOUNT_MIN <= len(value) <= CAMERA_ACCOUNT_MAX):
            raise ConfigError(
                f"camera-account {field_name} length must be "
                f"{CAMERA_ACCOUNT_MIN}-{CAMERA_ACCOUNT_MAX} chars, got {len(value)}",
                extra={"path": str(path), "field": field_name},
            )

    return ResolvedCredential(
        username=username,
        password=password,
        family="camera_account",
        source=source_label,
    )


def _read_json_v1(path: Path) -> dict[str, Any]:
    """Common loader: enforce permissions, parse JSON, validate v1 envelope.

    Returns the parsed payload dict. Caller pulls ``username``/``password``
    out (and validates length where relevant).
    """
    _enforce_permissions(path)

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(
            f"cannot read credentials file: {path} ({exc})",
            extra={"path": str(path)},
        ) from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"credentials file is not valid JSON: {path}: {exc.msg}",
            extra={"path": str(path)},
        ) from exc

    if not isinstance(payload, dict):
        raise ConfigError(
            f"credentials file root must be a JSON object: {path}",
            extra={"path": str(path)},
        )

    version = _coerce_version(payload, path)
    if version != CURRENT_VERSION:
        raise ConfigError(
            f"unsupported credentials file version {version} in {path}; "
            f"expected {CURRENT_VERSION}",
            hint="Run `tapo-cli auth migrate` to rewrite tapo-only files in place.",
            extra={"path": str(path), "version": version},
        )

    unknown = set(payload) - ALLOWED_CRED_KEYS_V1
    if unknown:
        raise ConfigError(
            f"unknown keys in credentials file {path}: {sorted(unknown)}",
            hint=f"Allowed: {sorted(ALLOWED_CRED_KEYS_V1)}",
            extra={"path": str(path), "unknown_keys": sorted(unknown)},
        )

    return payload


def _require_str(payload: dict[str, Any], key: str, path: Path) -> str:
    v = payload.get(key)
    if not isinstance(v, str) or not v:
        raise ConfigError(
            f"credentials file {path}: {key!r} must be a non-empty string",
            extra={"path": str(path)},
        )
    return v


def _coerce_version(payload: dict[str, Any], path: Path) -> int:
    """Pull ``version`` out, defaulting to 1 with a one-shot stderr warning."""
    if "version" in payload:
        v = payload["version"]
        if not isinstance(v, int) or isinstance(v, bool):
            raise ConfigError(
                f"credentials file {path}: 'version' must be an int, got {v!r}",
                extra={"path": str(path)},
            )
        return v

    key = str(path)
    with _DEPRECATION_LOCK:
        already = key in _DEPRECATION_WARNED
        if not already:
            _DEPRECATION_WARNED.add(key)
    if not already:
        logger.warning(
            "credentials file %s lacks a 'version' field; assuming version=1. "
            'Add `"version": 1` to silence this warning. '
            "`tapo-cli auth migrate` rewrites older tapo-only files in place.",
            path,
        )
    return 1


def _enforce_permissions(path: Path) -> None:
    """Reject permissive modes — and refuse symlinks outright (FR-CRED-2 / R5).

    A symlink to a 0600-mode target file would otherwise pass: ``path.stat()``
    follows the link and reads the target's mode. Refusing symlinks before
    any stat call ensures the actual file an operator sees in
    ``ls -l <path>`` is the one we audited.
    """
    if path.is_symlink():
        raise AuthError(
            f"credentials file {path} is a symlink; refusing for safety",
            hint="Replace the symlink with the actual file or use a per-device override.",
            extra={"path": str(path)},
        )
    info = path.stat()
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        raise AuthError(
            f"credentials file {path} has mode {oct(mode)}; expected 0600",
            hint=f"Run: chmod 600 {path}",
            extra={"path": str(path), "mode": oct(mode)},
        )


# ---------------------------------------------------------------------------
# Env-var path
# ---------------------------------------------------------------------------


def _from_env() -> ResolvedCredential | None:
    """Pull cloud creds from ``TAPO_USERNAME``/``TAPO_PASSWORD``.

    §6.2 partial-env-fall-through: if exactly ONE of the two is set, treat
    the source as "not set" and fall through. ``-v`` mode logs this once
    per process as a WARN line (`-v` controls handler level, not the WARN
    severity itself).
    """
    global _PARTIAL_ENV_WARNED
    user = os.environ.get(ENV_USERNAME) or ""
    pw = os.environ.get(ENV_PASSWORD) or ""

    if user and pw:
        return ResolvedCredential(
            username=user,
            password=pw,
            family="cloud_account",
            source="env",
        )

    if (user and not pw) or (pw and not user):
        with _DEPRECATION_LOCK:
            already = _PARTIAL_ENV_WARNED
            _PARTIAL_ENV_WARNED = True
        if not already:
            logger.warning(
                "partial credential env-vars: %s set but %s missing — "
                "treating env source as unset and falling through. "
                "Either set both or unset both.",
                ENV_USERNAME if user else ENV_PASSWORD,
                ENV_PASSWORD if user else ENV_USERNAME,
            )

    return None


def _expand(raw_path: str) -> Path:
    """Expand ``~`` and environment variables, returning a fully-resolved Path."""
    return Path(os.path.expandvars(raw_path)).expanduser()


# ---------------------------------------------------------------------------
# Test seam
# ---------------------------------------------------------------------------


def _reset_deprecation_state_for_tests() -> None:
    """Test-only: clear once-per-process latches."""
    global _PARTIAL_ENV_WARNED
    with _DEPRECATION_LOCK:
        _DEPRECATION_WARNED.clear()
        _PARTIAL_ENV_WARNED = False


__all__ = [
    "ALLOWED_CRED_KEYS_V1",
    "CAMERA_ACCOUNT_MAX",
    "CAMERA_ACCOUNT_MIN",
    "CURRENT_VERSION",
    "ENV_PASSWORD",
    "ENV_USERNAME",
    "KASA_SHARED_PATH",
    "TAPO_OVERRIDE_PATH",
    "CredentialSource",
    "default_cloud_file_path",
    "resolve_camera_account",
    "resolve_control_plane",
]
