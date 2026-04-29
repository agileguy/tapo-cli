"""``tapo-cli set <target> [--image-flip on|off] [--timezone <IANA>]`` (FR-39, FR-39a, FR-39c).

Phase 4a retro-fix: v0.2.0 (Phase 2) shipped without the ``set`` verb
despite §16.2 / FR-39 / FR-39a listing it as a Phase 2 acceptance item;
v1.2.0 audit catalogued the slip and ships it here in Phase 4a alongside
the fan-out generalization (FR-43d). Two flags are in scope per the SRD's
explicit v1 surface (FR-39b defers HDR / noise-cancelling / auto-track /
SD-recording to v0.4+):

* ``--image-flip on|off`` — pytapo ``setImageFlipVertical(enable: bool)``.
  When ``on``, the camera's image is rotated 180° on-device — useful for
  ceiling-mount installs where the camera is upside down. The pytapo path
  is ``__setImageSwitch("flip_type", "center"|"off")`` under the hood,
  not the more well-known ``setLensDistortionCorrection`` (which controls
  fisheye correction, not flip).
* ``--timezone <IANA>`` — pytapo ``setTimezone(timezone, zoneID, timingMode="ntp")``.
  Pytapo at the pinned SHA does NOT expose an IANA→zone_id lookup; we
  pass the IANA value as both fields. Current C-series firmware accepts
  this on every probe we've run; older firmware that wants a numeric
  zone_id will need a future enhancement.

Group fan-out (FR-43d): ``@group`` targets fan out per-camera with the
standard B10 envelope. Mixed-feature groups (FR-43f): per-target exit-5
on members lacking the requested capability.

JSON output shape (single-target):

    {
      "target": "<alias>",
      "changes": {
        "image_flip": true | false,        # present when --image-flip set
        "timezone":   "America/Vancouver"  # present when --timezone set
      }
    }

Group fan-out emits one such record per camera inside the standard
``{target, status, exit_code, result, error?}`` envelope (FR-44a / B10).
"""

from __future__ import annotations

import asyncio
import logging
import sys

import click

from tapo_cli.errors import EXIT_SUCCESS, UsageError
from tapo_cli.output import OutputMode, emit
from tapo_cli.runner import run_async as _run_async
from tapo_cli.verbs._target import load_config_with_target

logger = logging.getLogger("tapo_cli")


# ---------------------------------------------------------------------------
# Click verb
# ---------------------------------------------------------------------------


@click.command("set")
@click.argument("target", type=str)
@click.option(
    "--image-flip",
    "image_flip",
    type=click.Choice(["on", "off"]),
    default=None,
    help="Toggle image flip (FR-39). Pytapo setImageFlipVertical.",
)
@click.option(
    "--timezone",
    "timezone",
    type=str,
    default=None,
    help="Set the camera's timezone (FR-39a). IANA name, e.g. America/Vancouver.",
)
@click.pass_context
def set_cmd(
    ctx: click.Context,
    target: str,
    image_flip: str | None,
    timezone: str | None,
) -> None:
    """Apply one or more device-config changes (FR-39 / FR-39a, Phase 4a)."""
    if image_flip is None and timezone is None:
        raise UsageError(
            "set requires at least one of --image-flip or --timezone",
            target=target,
            hint=(
                "Pass --image-flip on|off, --timezone <IANA>, or both. "
                "Other knobs (HDR, noise cancelling, etc.) are deferred per FR-39b."
            ),
        )

    state = ctx.obj
    mode: OutputMode = state["mode"]
    timeout = float(state.get("timeout") or 5.0)
    config_path = state.get("config_path")
    credential_source = state.get("credential_source")
    concurrency = state.get("concurrency")

    rc = _run_async(
        lambda: _run(
            target=target,
            image_flip=image_flip,
            timezone=timezone,
            mode=mode,
            timeout=timeout,
            config_path=config_path,
            credential_source=credential_source,
            concurrency=concurrency,
        ),
        mode=mode,
    )
    sys.exit(rc)


async def _run(
    *,
    target: str,
    image_flip: str | None,
    timezone: str | None,
    mode: OutputMode,
    timeout: float,
    config_path: object,
    credential_source: object,
    concurrency: int | None = None,
) -> int:
    from tapo_cli.verbs._fanout import (
        group_members,
        is_group_target,
        run_fanout,
    )

    cfg, _ = load_config_with_target(target, config_path)
    if is_group_target(target, cfg):
        members = group_members(target, cfg)

        async def _per_target(alias: str) -> tuple[int, dict[str, object]]:
            record = await _execute_set(
                alias=alias,
                image_flip=image_flip,
                timezone=timezone,
                config_path=config_path,
                credential_source=credential_source,
                timeout=timeout,
            )
            return 0, record

        return await run_fanout(
            members=members,
            per_target=_per_target,
            concurrency=concurrency or cfg.defaults.concurrency,
            mode=mode,
        )

    record = await _execute_set(
        alias=target,
        image_flip=image_flip,
        timezone=timezone,
        config_path=config_path,
        credential_source=credential_source,
        timeout=timeout,
    )
    emit(record, mode, formatter=_to_text)
    return EXIT_SUCCESS


async def _execute_set(
    *,
    alias: str,
    image_flip: str | None,
    timezone: str | None,
    config_path: object,
    credential_source: object,
    timeout: float,
) -> dict[str, object]:
    from tapo_cli import wrapper as wrap

    cfg, resolved_target = load_config_with_target(alias, config_path)
    conn = await wrap.connect(
        cfg,
        resolved_target,
        credential_source=credential_source,  # type: ignore[arg-type]
        timeout=timeout,
    )

    canonical_alias = conn.target.alias
    changes: dict[str, object] = {}

    if image_flip is not None:
        flip_bool = image_flip == "on"
        await asyncio.to_thread(conn.tapo.setImageFlipVertical, flip_bool)
        changes["image_flip"] = flip_bool

    if timezone is not None:
        # Pytapo at the pinned SHA needs both ``timezone`` and ``zoneID``.
        # We pass IANA in both slots — current Tapo firmware accepts the
        # IANA name in zone_id; older builds may want a numeric id.
        await asyncio.to_thread(
            conn.tapo.setTimezone, timezone, timezone
        )
        changes["timezone"] = timezone

    return {"target": canonical_alias, "changes": changes}


def _to_text(record: object) -> str:
    if not isinstance(record, dict):
        return str(record)
    parts = [f"{record.get('target', '-')}"]
    changes = record.get("changes")
    if isinstance(changes, dict):
        for key, value in changes.items():
            parts.append(f"{key}={value}")
    return "\t".join(parts)


__all__ = ["set_cmd"]
