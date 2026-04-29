"""``tapo-cli info <target>`` (SRD §5.3, FR-9, FR-10, FR-CRED-8/8.1).

Resolves a single target (alias, ``@alias``, or bare IP) through the
wrapper, calls pytapo's ``getBasicInfo()`` on a worker thread (Phase 0
lesson: pytapo is sync), and emits the full §10.1 Camera record.

Auth fallback (FR-CRED-8): the wrapper already attempts the camera-
account credential first and falls back to cloud-account on
``_AUTH_FAILED``, emitting the FR-CRED-8.1 deprecation WARN. Two
consecutive auth failures bubble up as :class:`AuthError` (exit 2).

Group fan-out (FR-43d, FR-56): when ``target`` is a configured ``@group``,
expand to per-member fan-out via :mod:`tapo_cli.verbs._fanout` with the
B10 envelope. Phase 4a generalizes the contract; before Phase 4a only
``ptz`` honored ``@group``.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

import click

from tapo_cli.config import Config, DeviceEntry, load_config
from tapo_cli.device_info import (
    features_for_model,
    first_str,
    flatten_basic_info,
    format_mac,
    model_supported,
)
from tapo_cli.errors import EXIT_SUCCESS
from tapo_cli.output import OutputMode, emit, utc_now_rfc3339
from tapo_cli.runner import run_async as _run_async
from tapo_cli.types import Camera

logger = logging.getLogger("tapo_cli")


# ---------------------------------------------------------------------------
# Click verb
# ---------------------------------------------------------------------------


@click.command("info")
@click.argument("target", type=str)
@click.pass_context
def info_cmd(ctx: click.Context, target: str) -> None:
    """Show full state of one camera (or every member of an @group)."""
    state = ctx.obj
    mode: OutputMode = state["mode"]
    timeout = float(state.get("timeout") or 5.0)
    config_path = state.get("config_path")
    credential_source = state.get("credential_source")
    concurrency = state.get("concurrency")

    rc = _run_async(
        lambda: _run(
            target=target,
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
    mode: OutputMode,
    timeout: float,
    config_path: object,
    credential_source: object,
    concurrency: int | None = None,
) -> int:
    from pathlib import Path

    from tapo_cli.verbs._fanout import (
        group_members,
        is_group_target,
        run_fanout,
    )

    cfg_path = Path(str(config_path)).expanduser() if config_path else None
    cfg = load_config(cfg_path)

    # FR-43d / FR-56: group fan-out for @group targets.
    if is_group_target(target, cfg):
        members = group_members(target, cfg)

        async def _per_target(alias: str) -> tuple[int, dict[str, Any]]:
            camera = await _execute_info(
                alias=alias,
                config_path=config_path,
                credential_source=credential_source,
                timeout=timeout,
            )
            return 0, _camera_to_dict(camera)

        return await run_fanout(
            members=members,
            per_target=_per_target,
            concurrency=concurrency or cfg.defaults.concurrency,
            mode=mode,
        )

    # Single-target path (FR-9 / unchanged Phase 1b shape). Aliases,
    # ``@alias``, or bare IPs all acceptable; the leading ``@`` is stripped
    # if the bare token isn't itself a configured group name.
    record = await _execute_info(
        alias=target.lstrip("@") or target,
        config_path=config_path,
        credential_source=credential_source,
        timeout=timeout,
    )
    emit(record, mode, formatter=_camera_to_text)
    return EXIT_SUCCESS


async def _execute_info(
    *,
    alias: str,
    config_path: object,
    credential_source: object,
    timeout: float,
) -> Camera:
    """Run one ``info`` op against ``alias`` and return the §10.1 Camera record.

    Pulled out of :func:`_run` so the fan-out helper can reuse it
    per-member without the emit step.
    """
    from pathlib import Path

    from tapo_cli import wrapper as wrap

    cfg_path = Path(str(config_path)).expanduser() if config_path else None
    cfg = load_config(cfg_path)
    cfg = _ensure_target_resolvable(cfg, alias)

    conn = await wrap.connect(
        cfg,
        alias,
        credential_source=credential_source,  # type: ignore[arg-type]
        timeout=timeout,
    )

    def _basic() -> dict[str, Any]:
        result: object = conn.tapo.getBasicInfo()
        return result if isinstance(result, dict) else {}

    raw = await asyncio.to_thread(_basic)
    info = flatten_basic_info(raw)

    model = first_str(info, "device_model", "model", "device_type")
    return Camera(
        alias=conn.target.alias,
        ip=conn.target.ip,
        mac=format_mac(info.get("mac") or conn.target.mac or ""),
        model=model,
        hardware_version=first_str(info, "hw_version", "hardware_version"),
        firmware_version=first_str(info, "sw_version", "fw_version", "firmware_version"),
        supported=model_supported(model),
        motion_enabled=False,
        privacy_enabled=False,
        led_state="off",
        night_vision_mode="unknown",
        has_camera_account=bool(conn.target.camera_account_file),
        last_seen=utc_now_rfc3339(),
        features=features_for_model(model),
    )


def _camera_to_dict(camera: Camera) -> dict[str, Any]:
    """Project a :class:`Camera` dataclass onto a plain dict for JSONL fan-out."""
    return {
        "alias": camera.alias,
        "ip": camera.ip,
        "mac": camera.mac,
        "model": camera.model,
        "hardware_version": camera.hardware_version,
        "firmware_version": camera.firmware_version,
        "supported": camera.supported,
        "motion_enabled": camera.motion_enabled,
        "privacy_enabled": camera.privacy_enabled,
        "led_state": camera.led_state,
        "night_vision_mode": camera.night_vision_mode,
        "has_camera_account": camera.has_camera_account,
        "last_seen": camera.last_seen,
        "features": list(camera.features),
    }


def _ensure_target_resolvable(cfg: Config, target: str) -> Config:
    """If ``target`` is a bare IP not in config, synthesize a DeviceEntry."""
    if target in cfg.devices:
        return cfg
    if not _looks_like_ipv4(target):
        # Let the wrapper raise NotFoundError with the correct hint.
        return cfg
    cfg.devices[target] = DeviceEntry(alias=target, ip=target)
    return cfg


def _looks_like_ipv4(s: str) -> bool:
    parts = s.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def _camera_to_text(record: object) -> str:
    """Multi-line text rendering for ``info`` (TEXT mode only)."""
    if not isinstance(record, Camera):
        return str(record)
    lines = [
        f"alias:           {record.alias}",
        f"ip:              {record.ip}",
        f"mac:             {record.mac}",
        f"model:           {record.model}",
        f"hardware:        {record.hardware_version}",
        f"firmware:        {record.firmware_version}",
        f"supported:       {'yes' if record.supported else 'no'}",
        f"features:        {', '.join(record.features) or '-'}",
        f"motion-enabled:  {record.motion_enabled}",
        f"privacy-enabled: {record.privacy_enabled}",
        f"led-state:       {record.led_state}",
        f"night-vision:    {record.night_vision_mode}",
        f"camera-account:  {'yes' if record.has_camera_account else 'no'}",
        f"last-seen:       {record.last_seen}",
    ]
    return "\n".join(lines)


__all__ = ["info_cmd"]
