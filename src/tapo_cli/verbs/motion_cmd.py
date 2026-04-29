"""``tapo-cli motion <target> enable|disable|status|history`` (SRD §5.x).

Phase 1d implements ``enable``, ``disable``, and ``status``. The
``history`` sub-verb is wired in but exits 5 (unsupported feature in
this phase) with a hint pointing to Phase 3 — the motion-event timeline
needs the on-camera SD-card index reader pytapo doesn't yet expose
cleanly enough for v1.

Pytapo's ``setMotionDetection``/``getMotionDetection`` operate on the
``motion_detection.motion_det`` config table:

* ``setMotionDetection(enabled=bool, sensitivity=False, chn_id=None)``
* ``getMotionDetection()`` returns ``{"enabled": "on"|"off",
  "digital_sensitivity": "20"|"40"|"60"|"80", "sensitivity": "low"|...}``

For ``enable``/``disable`` we pass ``enabled=bool`` and let pytapo round-
trip the existing sensitivity. ``status`` reports the boolean plus the
device-reported sensitivity so dashboards can render both without
re-querying.

Output shape:
  ``{target, motion_enabled: bool, sensitivity?: str}``
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

import click

from tapo_cli.errors import EXIT_SUCCESS, UnsupportedFeatureError
from tapo_cli.output import OutputMode, emit
from tapo_cli.runner import run_async as _run_async
from tapo_cli.verbs._target import load_config_with_target

logger = logging.getLogger("tapo_cli")


# ---------------------------------------------------------------------------
# Click verb
# ---------------------------------------------------------------------------


@click.command("motion")
@click.argument("target", type=str)
@click.argument(
    "action",
    type=click.Choice(["enable", "disable", "status", "history"]),
)
@click.pass_context
def motion_cmd(ctx: click.Context, target: str, action: str) -> None:
    """Enable, disable, or report motion detection (history → Phase 3)."""
    state = ctx.obj
    mode: OutputMode = state["mode"]
    timeout = float(state.get("timeout") or 5.0)
    config_path = state.get("config_path")
    credential_source = state.get("credential_source")

    rc = _run_async(
        lambda: _run(
            target=target,
            action=action,
            mode=mode,
            timeout=timeout,
            config_path=config_path,
            credential_source=credential_source,
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
) -> int:
    if action == "history":
        # Phase 1d does NOT implement motion-history; fail fast with the
        # canonical "feature not in this phase" exit code 5.
        raise UnsupportedFeatureError(
            "motion history is not implemented in Phase 1d",
            target=target,
            hint="Motion-event history lands in Phase 3 (FR-25/FR-26).",
        )

    from tapo_cli import wrapper as wrap

    cfg, resolved_target = load_config_with_target(target, config_path)
    conn = await wrap.connect(
        cfg,
        resolved_target,
        credential_source=credential_source,  # type: ignore[arg-type]
        timeout=timeout,
    )

    sensitivity: str | None = None
    if action == "enable":
        await asyncio.to_thread(conn.tapo.setMotionDetection, True)
        motion_enabled = True
    elif action == "disable":
        await asyncio.to_thread(conn.tapo.setMotionDetection, False)
        motion_enabled = False
    else:  # status
        motion_enabled, sensitivity = await asyncio.to_thread(
            _read_motion_state, conn.tapo
        )

    record: dict[str, object] = {
        "target": conn.target.alias,
        "motion_enabled": motion_enabled,
    }
    if sensitivity is not None:
        record["sensitivity"] = sensitivity

    emit(record, mode, formatter=_to_text)
    return EXIT_SUCCESS


def _read_motion_state(tapo: Any) -> tuple[bool, str | None]:
    """Return ``(enabled, sensitivity)`` from pytapo's getMotionDetection.

    Sensitivity is best-effort — older firmware emits ``digital_sensitivity``
    (a numeric string), newer firmware emits both ``digital_sensitivity``
    and ``sensitivity`` (a string label). We prefer the label; fall back
    to the numeric. ``None`` means the field wasn't present.
    """
    raw: object = tapo.getMotionDetection()
    if not isinstance(raw, dict):
        return False, None

    value = raw.get("enabled")
    enabled: bool
    if isinstance(value, str):
        enabled = value.lower() == "on"
    elif isinstance(value, bool):
        enabled = value
    else:
        enabled = False

    sensitivity: str | None = None
    label = raw.get("sensitivity")
    if isinstance(label, str) and label:
        sensitivity = label
    else:
        digital = raw.get("digital_sensitivity")
        if isinstance(digital, (str, int)):
            sensitivity = str(digital)

    return enabled, sensitivity


def _to_text(record: object) -> str:
    if not isinstance(record, dict):
        return str(record)
    parts = [
        f"{record.get('target', '-')}",
        f"motion={record.get('motion_enabled')}",
    ]
    if "sensitivity" in record:
        parts.append(f"sensitivity={record['sensitivity']}")
    return "\t".join(parts)


__all__ = ["motion_cmd"]
