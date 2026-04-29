"""Thin pytapo wrapper for tapo-cli.

This module is the ONLY place in the project that imports ``pytapo``. Verb
modules call ``wrapper.*`` exclusively — they never poke at pytapo
directly. Keeping the boundary narrow means the rest of the codebase stays
testable with simple mocks and that any future pytapo churn lands in a
single file.

Phase 1a ships only the *plumbing* — alias-to-device resolution, an opaque
session-cache wrapper, the camera-account-first auth chain with
cloud-account fallback per FR-CRED-8/8.1, and the
``asyncio.to_thread``-based pytapo invocation. The actual control-plane
verbs (``info``, ``snapshot``, ``stream``, ``ptz``, …) ship in Phase 1b/1c/1d.

Design notes:

* pytapo at our pinned SHA is **synchronous** — it carries its own
  ``AsyncHandler`` that internally drives ``loop.run_until_complete``.
  Calling pytapo directly from inside an outer ``asyncio.run`` raises
  ``RuntimeError: Cannot run the event loop while another loop is running``
  (Phase 0 BUG 1, fixed in ``scripts/smoke.py``). All pytapo invocations
  here go through ``asyncio.to_thread`` so pytapo's loop is isolated to a
  worker thread.
* The wrapper does NOT touch the cache itself — :mod:`tapo_cli.auth_cache`
  owns persistence and locking. The wrapper hands an already-resolved
  state blob in / out and lets the cache layer lock around the write.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tapo_cli import auth_cache
from tapo_cli.config import Config
from tapo_cli.credentials import CredentialSource, resolve_control_plane
from tapo_cli.errors import (
    AuthError,
    NetworkError,
    NotFoundError,
)
from tapo_cli.types import ResolvedCredential

if TYPE_CHECKING:
    from pytapo import Tapo  # type: ignore[import-untyped]

logger = logging.getLogger("tapo_cli")


# ---------------------------------------------------------------------------
# Resolved target descriptor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TapoTarget:
    """One device after alias-to-config resolution.

    Carries everything the wrapper needs to authenticate and hit the
    camera: IP, MAC, model (if known), and credential file references for
    both families. The wrapper applies the §6.2 chain at connect time —
    callers don't pre-resolve credentials.
    """

    alias: str
    ip: str
    mac: str | None = None
    model: str | None = None
    camera_account_file: str | None = None
    credential_file: str | None = None


def resolve_target(config: Config, target: str) -> TapoTarget:
    """Resolve a CLI target string into a :class:`TapoTarget`.

    Phase 1a only handles config-defined aliases. Phase 1b adds raw IP /
    MAC discovery via ``discover``. Group expansion happens at the
    verb-handler layer (``@group``) — this function rejects group syntax.

    Raises:
        NotFoundError: alias not found in config and target is not a
            literal IP address.
    """
    if target.startswith("@"):
        raise NotFoundError(
            f"group target {target!r} cannot be resolved as a single device",
            target=target,
            hint="Group expansion happens at the verb layer, not in the wrapper.",
        )

    entry = config.devices.get(target)
    if entry is None:
        # Phase 1a: bare-IP / bare-MAC targeting is a Phase 1b concern (needs
        # discover-side support to learn the MAC). For now, surface a clear
        # config error.
        raise NotFoundError(
            f"unknown alias: {target!r}",
            target=target,
            hint=(
                "Add [devices." + target + "] to ~/.config/tapo-cli/config.toml, "
                "or run `tapo-cli config show` to list known aliases."
            ),
        )

    if not entry.ip:
        raise NotFoundError(
            f"alias {target!r} has no ip in config",
            target=target,
            hint=f"Add `ip = \"<address>\"` under [devices.{target}].",
        )

    return TapoTarget(
        alias=entry.alias,
        ip=entry.ip,
        mac=entry.mac,
        model=entry.model,
        camera_account_file=entry.camera_account_file,
        credential_file=entry.credential_file,
    )


# ---------------------------------------------------------------------------
# pytapo bring-up (camera-account-first, cloud fallback per FR-CRED-8/8.1)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TapoConnection:
    """Live pytapo handle plus the credential family that authenticated it.

    ``credential_family`` records which family produced the handshake so
    callers (cache writer, ``-vv`` logging) can tag the cache file
    appropriately for FR-CRED-11 invalidation.
    """

    tapo: Tapo
    target: TapoTarget
    credential_family: str
    """``"camera_account"`` or ``"cloud_account"``."""


async def connect(
    config: Config,
    target: str,
    *,
    credential_source: CredentialSource | None = None,
    timeout: float = 5.0,
) -> TapoConnection:
    """Bring up a pytapo connection per FR-CRED-8 (camera-first, cloud fallback).

    Args:
        config: Active config.
        target: Alias to resolve.
        credential_source: ``--credential-source`` value or ``None`` for
            the default §6.2 chain.
        timeout: Per-call timeout. Currently informational — pytapo
            doesn't expose a wire-level timeout knob; the value is logged
            for ``-vv`` mode and reserved for future use.

    Raises:
        AuthError: when no credential source produced a usable login.
        NetworkError: on connection-level failures.
    """
    del timeout  # reserved; pytapo lacks an explicit wire-timeout knob

    tt = resolve_target(config, target)
    primary = resolve_control_plane(config, alias=target, source=credential_source)

    if primary is None:
        raise AuthError(
            f"no credentials available for alias {target!r}",
            target=target,
            hint=(
                "Configure either a camera_account_file (preferred) or "
                "cloud-account credentials at ~/.config/kasa-cli/credentials."
            ),
        )

    # Try the primary credential. On _AUTH_FAILED from a camera-account
    # primary, fall back to a cloud-account credential if one's available.
    try:
        tapo = await _build_tapo(tt, primary)
    except AuthError:
        if primary.family == "camera_account":
            cloud = _force_cloud_account(config, target, credential_source)
            if cloud is not None:
                logger.warning(
                    "cloud-account fallback used for %s; camera-account login is the "
                    "supported path on current firmware. See SRD §6.1.",
                    target,
                )
                tapo = await _build_tapo(tt, cloud)
                return TapoConnection(tapo=tapo, target=tt, credential_family="cloud_account")
        raise

    return TapoConnection(tapo=tapo, target=tt, credential_family=primary.family)


def _force_cloud_account(
    config: Config,
    alias: str,
    credential_source: CredentialSource | None,
) -> ResolvedCredential | None:
    """Walk the chain again skipping camera-account files.

    Used by the FR-CRED-8 fallback path. Honors ``--credential-source``:
    ``env`` / ``none`` short-circuit to ``None`` since they cannot produce
    a cloud-account file anyway (env yields cloud, but the failed primary
    was already env or file-cloud).
    """
    if credential_source == "none":
        return None
    # Resolve again WITHOUT the alias so per-device camera-account isn't
    # consulted; the caller already exhausted that path.
    cred = resolve_control_plane(config, alias=None, source=credential_source)
    if cred is None or cred.family != "cloud_account":
        return None
    return cred


async def _build_tapo(target: TapoTarget, cred: ResolvedCredential) -> Tapo:
    """Construct a pytapo Tapo handle on a worker thread.

    pytapo at the pinned SHA is sync; calling it on the asyncio loop thread
    deadlocks. ``asyncio.to_thread`` isolates pytapo's internal handler.

    Auth fallback inside pytapo is a separate concern from FR-CRED-8 —
    pytapo will retry its own auth variants (legacy POST, KLAP, SSE) from
    a single (user, pass) pair. We branch on credential family at this
    layer.
    """
    try:
        # Lazy import — keeps the rest of the module testable without pytapo
        # actually installed. The wrapper layer is the ONLY place pytapo is
        # imported.
        from pytapo import Tapo as _Tapo
    except ImportError as exc:
        raise NetworkError(
            f"pytapo is not installed: {exc}",
            hint="Run `uv sync --all-extras --dev` to install it.",
        ) from exc

    def _ctor() -> Tapo:
        try:
            return _Tapo(target.ip, cred.username, cred.password)
        except Exception as exc:  # pytapo raises a wide variety
            msg = str(exc).lower()
            if "auth" in msg or "401" in msg or "_auth_failed" in msg:
                raise AuthError(
                    f"pytapo auth failed for {target.alias} via {cred.family}",
                    target=target.alias,
                    credential=cred.family,
                    mechanism="pytapo",
                    extra={"underlying": str(exc)},
                ) from exc
            raise NetworkError(
                f"pytapo connection failed for {target.alias}: {exc}",
                target=target.alias,
                mechanism="pytapo",
            ) from exc

    return await asyncio.to_thread(_ctor)


# ---------------------------------------------------------------------------
# Cache integration helpers (used by Phase 1b verbs)
# ---------------------------------------------------------------------------


def cache_blob_for(target: TapoTarget) -> dict[str, Any] | None:
    """Convenience: load a cached pytapo state blob for a resolved target.

    Returns ``None`` when the MAC is unknown (alias has no MAC in config) or
    when the cache is empty/stale. Intended for verb modules that want to
    short-circuit the pytapo handshake when a fresh cache exists.
    """
    if not target.mac:
        return None
    return auth_cache.load_session(target.mac)


__all__ = [
    "TapoConnection",
    "TapoTarget",
    "cache_blob_for",
    "connect",
    "resolve_target",
]
