"""``tapo-cli alarm <target> ...`` (FR-22..24, FR-26..29).

Four sub-verbs covering the camera's siren / alarm:

* ``alarm enable``  — turn alarm-on-motion-event ON (FR-22, FR-26)
* ``alarm disable`` — turn alarm-on-motion-event OFF (FR-23, FR-27)
* ``alarm trigger`` — fire the manual siren immediately (FR-28)
* ``alarm status``  — read current alarm config (FR-24, FR-29)

Capability gating (S4):

* ``enable``/``disable``/``status`` consult the ``alarm`` capability flag.
* ``trigger`` consults the ``alarm_trigger`` capability flag (a stricter
  subset — many models can be configured to alarm on motion but lack the
  manual-trigger pytapo verb). Live C200 has ``alarm: true`` AND
  ``alarm_trigger: false``, so ``trigger`` exits 5 on it; ``enable``,
  ``disable``, and ``status`` succeed.

pytapo signatures verified at the pinned SHA:

* ``setAlarm(enabled, soundEnabled=True, lightEnabled=True, alarmVolume=None,
  alarmDuration=None, alarmType=None)``
* ``getAlarm()`` — returns the current alarm config dict.
* ``startManualAlarm()`` / ``stopManualAlarm()`` — manual fire/cancel.

JSON output shape (FR-22..24, FR-26..29):

    {
      "target": "<alias>",
      "action": "enable|disable|trigger|status",
      "alarm_enabled": <bool>,
      "sound_enabled": <bool>,        # present when device reports it
      "light_enabled": <bool>         # present when device reports it
    }

The verb returns immediately after issuing the request — we do NOT sleep
through the alarm duration. Operators wanting to wait/pause should chain
``trigger`` with their own ``sleep`` invocation; the CLI is a leaf node.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

import click

from tapo_cli.errors import EXIT_SUCCESS
from tapo_cli.output import OutputMode, emit
from tapo_cli.runner import run_async as _run_async
from tapo_cli.verbs._capability import require_feature, resolve_model_for_target
from tapo_cli.verbs._target import load_config_with_target

logger = logging.getLogger("tapo_cli")


# ---------------------------------------------------------------------------
# Click verb
# ---------------------------------------------------------------------------


@click.command("alarm")
@click.argument("target", type=str)
@click.argument(
    "action",
    type=click.Choice(["enable", "disable", "trigger", "status"]),
)
@click.pass_context
def alarm_cmd(ctx: click.Context, target: str, action: str) -> None:
    """Enable / disable / trigger / status the camera's alarm/siren.

    Group fan-out (FR-43d): ``@group`` targets fan out per-camera with
    the standard B10 envelope. FR-43f mixed-feature behavior: members
    lacking the requested feature emit a per-target exit-5 result.
    """
    state = ctx.obj
    mode: OutputMode = state["mode"]
    timeout = float(state.get("timeout") or 5.0)
    config_path = state.get("config_path")
    credential_source = state.get("credential_source")
    concurrency = state.get("concurrency")

    rc = _run_async(
        lambda: _run(
            target=target,
            action=action,
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
    action: str,
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
            record = await _execute_alarm(
                alias=alias,
                action=action,
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

    record = await _execute_alarm(
        alias=target,
        action=action,
        config_path=config_path,
        credential_source=credential_source,
        timeout=timeout,
    )
    emit(record, mode, formatter=_to_text)
    return EXIT_SUCCESS


async def _execute_alarm(
    *,
    alias: str,
    action: str,
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

    config_entry = cfg.devices.get(resolved_target)
    config_model = config_entry.model if config_entry is not None else None
    model = await resolve_model_for_target(conn.tapo, config_model=config_model)

    # Capability gate per S4.
    feature = "alarm_trigger" if action == "trigger" else "alarm"
    require_feature(
        model=model, target=conn.target.alias, feature=feature, verb_name=f"alarm {action}"
    )

    canonical_alias = conn.target.alias

    if action == "enable":
        await asyncio.to_thread(_set_alarm, conn.tapo, True)
        return await _build_status_record(conn.tapo, canonical_alias, action="enable")
    if action == "disable":
        await asyncio.to_thread(_set_alarm, conn.tapo, False)
        return await _build_status_record(conn.tapo, canonical_alias, action="disable")
    if action == "trigger":
        await asyncio.to_thread(conn.tapo.startManualAlarm)
        return {
            "target": canonical_alias,
            "action": "trigger",
            "alarm_enabled": True,
            "manual": True,
        }
    # status
    return await _build_status_record(conn.tapo, canonical_alias, action="status")


def _set_alarm(tapo: Any, enabled: bool) -> None:
    """Drive ``setAlarm`` with sensible defaults.

    pytapo's signature is ``setAlarm(enabled, soundEnabled=True,
    lightEnabled=True, ...)``. On disable we still pass the truthy
    sound/light flags — those configure WHAT the alarm does when it
    fires, not whether it can fire. Operators tuning per-channel
    behavior should use the device's app for now (Phase 2 doesn't
    expose ``--sound``/``--light`` flags).
    """
    tapo.setAlarm(enabled, True, True)


async def _build_status_record(
    tapo: Any, alias: str, *, action: str
) -> dict[str, object]:
    """Read ``getAlarm`` and project it onto our response shape."""
    raw: object = await asyncio.to_thread(tapo.getAlarm)
    record: dict[str, object] = {"target": alias, "action": action}

    if isinstance(raw, dict):
        # pytapo / firmware shapes vary. Common keys:
        #   "enabled": "on"|"off"|bool
        #   "alarm_mode": ["sound","light"] OR string
        #   "msg_alarm": {"enabled": "...", "alarm_type":...}
        enabled_value = raw.get("enabled")
        if enabled_value is None and isinstance(raw.get("msg_alarm"), dict):
            enabled_value = raw["msg_alarm"].get("enabled")
        record["alarm_enabled"] = _coerce_bool(enabled_value)

        modes_raw = raw.get("alarm_mode")
        if modes_raw is None and isinstance(raw.get("msg_alarm"), dict):
            modes_raw = raw["msg_alarm"].get("alarm_mode")

        if isinstance(modes_raw, list):
            modes = {str(m).lower() for m in modes_raw}
            record["sound_enabled"] = "sound" in modes
            record["light_enabled"] = "light" in modes
        elif isinstance(modes_raw, str):
            modes = {part.strip().lower() for part in modes_raw.split(",")}
            record["sound_enabled"] = "sound" in modes
            record["light_enabled"] = "light" in modes
    else:
        record["alarm_enabled"] = False

    return record


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"on", "true", "1", "enable", "enabled", "yes"}
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _to_text(record: object) -> str:
    if not isinstance(record, dict):
        return str(record)
    parts = [
        f"{record.get('target', '-')}",
        f"action={record.get('action')}",
        f"alarm_enabled={record.get('alarm_enabled')}",
    ]
    if "sound_enabled" in record:
        parts.append(f"sound_enabled={record['sound_enabled']}")
    if "light_enabled" in record:
        parts.append(f"light_enabled={record['light_enabled']}")
    if record.get("manual"):
        parts.append("manual=true")
    return "\t".join(parts)


__all__ = ["alarm_cmd"]
