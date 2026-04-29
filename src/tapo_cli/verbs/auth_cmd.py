"""``tapo-cli auth`` sub-verbs (FR-CRED-12, 14, 15a; SRD §6.6).

Three actions, no camera I/O:

* ``auth status`` — print one row per cached pytapo state file with alias,
  MAC, cache_path, mtime, file size, expires_at (RFC 3339 or null),
  pytapo_version, and the two ``cloud_account`` / ``camera_account``
  configured booleans (FR-CRED-14, S8).
* ``auth flush [--target ALIAS|MAC]`` — delete all cached state files,
  or just the one belonging to the given alias / MAC (FR-CRED-12).
* ``auth migrate`` — rewrite older versioned credential files in place at
  ``~/.config/tapo-cli/credentials`` ONLY. Refuses to touch the shared
  kasa-cli file (FR-CRED-3.1, FR-CRED-15a).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import click

from tapo_cli import auth_cache
from tapo_cli.config import Config, load_config
from tapo_cli.credentials import (
    ALLOWED_CRED_KEYS_V1,
    CURRENT_VERSION,
    KASA_SHARED_PATH,
    TAPO_OVERRIDE_PATH,
)
from tapo_cli.errors import (
    EXIT_SUCCESS,
    ConfigError,
    TapoCliError,
)
from tapo_cli.output import emit_error, emit_stream

logger = logging.getLogger("tapo_cli")


# ---------------------------------------------------------------------------
# auth status (FR-CRED-14)
# ---------------------------------------------------------------------------


def _alias_for_mac(config: Config, mac: str) -> str | None:
    """Find the alias whose configured MAC matches (case-insensitive)."""
    norm = mac.replace("-", "").replace(":", "").upper()
    for alias, entry in config.devices.items():
        if entry.mac is None:
            continue
        cand = entry.mac.replace("-", "").replace(":", "").upper()
        if cand == norm:
            return alias
    return None


def _row_for_cache_file(path: Path, config: Config) -> dict[str, Any]:
    """Build one ``auth status`` row from a cache file path."""
    mac = path.stem
    meta = auth_cache.read_session_meta(path)
    alias = _alias_for_mac(config, mac)
    cloud_configured = _cloud_configured(config, alias)
    camera_configured = _camera_configured(config, alias)

    stat_info = path.stat()
    return {
        "alias": alias,
        "mac": mac,
        "cache_path": str(path),
        "mtime": auth_cache.mtime_rfc3339(path),
        "bytes_size": stat_info.st_size,
        "expires_at": meta.get(auth_cache.KEY_EXPIRES_AT),
        "pytapo_version": meta.get(auth_cache.KEY_PYTAPO_VERSION, "unknown"),
        "cloud_account": cloud_configured,
        "camera_account": camera_configured,
    }


def _cloud_configured(config: Config, alias: str | None) -> bool:
    """True iff a cloud-account credential is configured for the alias.

    Counts: per-device ``credential_file`` (override) OR a default cloud
    file present at the FR-CRED-3.1 resolution paths.
    """
    if alias is not None:
        entry = config.devices.get(alias)
        if entry is not None and entry.credential_file:
            return Path(os.path.expandvars(entry.credential_file)).expanduser().exists()
    # Fall back: a default-path file present anywhere in the chain.
    for raw in (TAPO_OVERRIDE_PATH, config.credentials.file_path, KASA_SHARED_PATH):
        if Path(os.path.expandvars(raw)).expanduser().exists():
            return True
    return False


def _camera_configured(config: Config, alias: str | None) -> bool:
    if alias is None:
        return False
    entry = config.devices.get(alias)
    if entry is None or not entry.camera_account_file:
        return False
    return Path(os.path.expandvars(entry.camera_account_file)).expanduser().exists()


def _status_row_to_text(row: object) -> str:
    """Single-line text rendering."""
    assert isinstance(row, dict)
    return (
        f"{row.get('alias') or '-':<16} "
        f"{row.get('mac', '-'):<17} "
        f"{row.get('bytes_size', 0):>6}B "
        f"{row.get('mtime', '-')} "
        f"cloud={'y' if row.get('cloud_account') else 'n'} "
        f"cam={'y' if row.get('camera_account') else 'n'}"
    )


@click.group("auth")
def auth_group() -> None:
    """Authentication / pytapo session-cache sub-verbs."""


@auth_group.command("status")
@click.pass_context
def auth_status_cmd(ctx: click.Context) -> None:
    """Print one row per cached pytapo session state file."""
    state = ctx.obj
    try:
        config = load_config(_config_path(state))
    except TapoCliError as exc:
        emit_error(exc.to_structured(), state["mode"])
        sys.exit(exc.exit_code)

    rows = [_row_for_cache_file(p, config) for p in auth_cache.list_session_files()]
    emit_stream(rows, state["mode"], formatter=_status_row_to_text)
    sys.exit(EXIT_SUCCESS)


# ---------------------------------------------------------------------------
# auth flush (FR-CRED-12)
# ---------------------------------------------------------------------------


@auth_group.command("flush")
@click.option(
    "--target",
    type=str,
    default=None,
    help="Alias or MAC to flush. Default: flush all cached sessions.",
)
@click.pass_context
def auth_flush_cmd(ctx: click.Context, *, target: str | None) -> None:
    """Delete cached pytapo session state files."""
    state = ctx.obj
    if target is None:
        deleted = auth_cache.flush_all()
        click.echo(f"flushed {deleted} session file(s)")
        sys.exit(EXIT_SUCCESS)

    # Resolve alias→MAC if the config has a matching alias entry. We try the
    # config first regardless of shape — a 12-hex-char alias would be unusual
    # but valid, so config wins over format-guessing.
    mac = target
    try:
        config = load_config(_config_path(state))
    except TapoCliError as exc:
        emit_error(exc.to_structured(), state["mode"])
        sys.exit(exc.exit_code)
    entry = config.devices.get(target)
    if entry is not None and entry.mac is not None:
        mac = entry.mac

    deleted = 1 if auth_cache.flush_one(mac) else 0
    click.echo(f"flushed {deleted} session file(s)")
    sys.exit(EXIT_SUCCESS)


# ---------------------------------------------------------------------------
# auth migrate (FR-CRED-15a, FR-CRED-3.1)
# ---------------------------------------------------------------------------


@auth_group.command("migrate")
@click.pass_context
def auth_migrate_cmd(ctx: click.Context) -> None:
    """Rewrite older versioned credential files in place at the tapo-only path.

    SHALL act ONLY on ``~/.config/tapo-cli/credentials``. The shared
    ``~/.config/kasa-cli/credentials`` file is OWNED by kasa-cli per
    FR-CRED-3.1 and SHALL NOT be modified by tapo-cli.
    """
    state = ctx.obj
    target = Path(os.path.expandvars(TAPO_OVERRIDE_PATH)).expanduser()

    if not target.exists():
        click.echo(f"no tapo-only credentials file at {target}; nothing to migrate")
        sys.exit(EXIT_SUCCESS)

    # Defensive: refuse to ever touch the shared kasa file even if a
    # symlink chain points at it.
    if target.is_symlink():
        resolved = target.resolve()
        kasa_path = Path(os.path.expandvars(KASA_SHARED_PATH)).expanduser().resolve()
        if resolved == kasa_path:
            err = ConfigError(
                f"refusing to migrate: {target} is a symlink to the shared "
                f"kasa-cli credentials file at {kasa_path}",
                hint="tapo-cli SHALL NEVER write the kasa-cli file (FR-CRED-3.1).",
            )
            emit_error(err.to_structured(), state["mode"])
            sys.exit(err.exit_code)

    try:
        raw = target.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        err = ConfigError(
            f"cannot read {target} for migration: {exc}",
            extra={"path": str(target)},
        )
        emit_error(err.to_structured(), state["mode"])
        sys.exit(err.exit_code)

    if not isinstance(payload, dict):
        err = ConfigError(
            f"credentials file root must be a JSON object: {target}",
            extra={"path": str(target)},
        )
        emit_error(err.to_structured(), state["mode"])
        sys.exit(err.exit_code)

    # Phase 1a: only one schema version exists, so "migration" is a no-op
    # plus a version-stamp on files missing the field. Add the version key
    # if absent; preserve all known v1 keys; drop unknown keys with a warn.
    new_payload: dict[str, Any] = {"version": CURRENT_VERSION}
    for key in ("username", "password"):
        if key in payload:
            new_payload[key] = payload[key]

    dropped = set(payload) - ALLOWED_CRED_KEYS_V1
    if dropped:
        logger.warning(
            "auth migrate: dropping unknown keys from %s: %s",
            target,
            sorted(dropped),
        )

    # Atomic rewrite: tmpfile + fsync + rename.
    import contextlib
    import tempfile

    fd, tmp_str = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(new_payload, fh, separators=(",", ":"), sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise

    click.echo(f"migrated {target} to schema v{CURRENT_VERSION}")
    sys.exit(EXIT_SUCCESS)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _config_path(state: dict[str, Any]) -> Path | None:
    """Pull the optional --config path out of the Click context state."""
    raw = state.get("config_path")
    if raw is None:
        return None
    return Path(raw).expanduser()


__all__ = ["auth_group"]
