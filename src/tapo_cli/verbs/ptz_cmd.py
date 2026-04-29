"""``tapo-cli ptz <target> ...`` (FR-14..17, B7).

Five sub-verbs covering the camera's pan / tilt / zoom motors:

* ``pan {left,right} [--step N]``
* ``tilt {up,down} [--step N]``
* ``zoom {in,out} [--step N]``
* ``move --pan ±N --tilt ±N [--zoom ±N]``
* ``stop``

Unit semantics (B7) depend on the target's ``ptz_mode`` per the §3.3.1
capability matrix:

* ``ptz_mode: continuous`` — ``--step`` is interpreted as **degrees**.
  Pytapo's :meth:`Tapo.moveMotor` accepts degree-addressed offsets directly.
* ``ptz_mode: step`` — ``--step`` is interpreted as **device-step-units**.
  Pytapo's :meth:`Tapo.moveMotor` accepts integer step-units on these
  models too — pytapo doesn't expose a degree/step distinction; the CLI
  honors the SRD by labeling the unit in the JSON output (``step_unit``)
  so callers can record what they asked for without re-deriving it from
  the model.

Zoom ``--step`` is **always device-step-units** regardless of ``ptz_mode``
— there is no documented degree-mapping for zoom. Models without zoom
exit code 5.

``stop`` calls :meth:`Tapo.setMotorOff` if available; otherwise it issues
a zero-magnitude :meth:`Tapo.moveMotor(0, 0)` as a no-op cancellation.
The verb's contract is "halt any in-progress motion immediately" — which
on Tapo C-series firmware is a best-effort no-op since motion calls are
synchronous and complete before the next command. We document the result
as a JSON ``stopped: true`` so callers can assert the verb ran without
asserting wire-level effect.

JSON output shape (FR-17c):

    {
      "target": "<alias>",
      "action": "pan|tilt|zoom|move|stop",
      "step": <int>,            # omitted for "stop"
      "step_unit": "degrees|device-step-units",  # omitted for "stop"
      "elapsed_ms": <int>
    }

Group target fan-out (``@group``) is **NOT** wired in Phase 2 — it lands
in Phase 3 alongside the rest of the parallel-execution layer. Phase 2
treats ``@alias`` as a single alias (the leading ``@`` is stripped) per
:mod:`tapo_cli.verbs._target`.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any

import click

from tapo_cli.errors import EXIT_SUCCESS
from tapo_cli.output import OutputMode, emit
from tapo_cli.runner import run_async as _run_async
from tapo_cli.verbs._capability import require_ptz, resolve_model_for_target
from tapo_cli.verbs._target import load_config_with_target

logger = logging.getLogger("tapo_cli")

_DEFAULT_STEP: int = 10


# ---------------------------------------------------------------------------
# Click verb tree
# ---------------------------------------------------------------------------


@click.group("ptz")
@click.argument("target", type=str)
@click.pass_context
def ptz_cmd(ctx: click.Context, target: str) -> None:
    """Pan/tilt/zoom and stop sub-verbs (FR-14..17)."""
    ctx.obj = dict(ctx.obj or {})
    ctx.obj["__ptz_target__"] = target


@ptz_cmd.command("pan")
@click.argument("direction", type=click.Choice(["left", "right"]))
@click.option(
    "--step",
    "step",
    type=int,
    default=_DEFAULT_STEP,
    show_default=True,
    help="Pan magnitude (degrees on continuous-PTZ models, device-step-units on step models).",
)
@click.pass_context
def ptz_pan(ctx: click.Context, direction: str, step: int) -> None:
    """Move the camera horizontally (FR-14)."""
    _run_motion(ctx, action="pan", direction=direction, step=step)


@ptz_cmd.command("tilt")
@click.argument("direction", type=click.Choice(["up", "down"]))
@click.option(
    "--step",
    "step",
    type=int,
    default=_DEFAULT_STEP,
    show_default=True,
    help="Tilt magnitude (degrees on continuous-PTZ models, device-step-units on step models).",
)
@click.pass_context
def ptz_tilt(ctx: click.Context, direction: str, step: int) -> None:
    """Move the camera vertically (FR-15)."""
    _run_motion(ctx, action="tilt", direction=direction, step=step)


@ptz_cmd.command("zoom")
@click.argument("direction", type=click.Choice(["in", "out"]))
@click.option(
    "--step",
    "step",
    type=int,
    default=_DEFAULT_STEP,
    show_default=True,
    help="Zoom magnitude (always device-step-units; no degree mapping for zoom).",
)
@click.pass_context
def ptz_zoom(ctx: click.Context, direction: str, step: int) -> None:
    """Zoom the camera in/out (FR-16). Always device-step-units."""
    _run_motion(ctx, action="zoom", direction=direction, step=step)


@ptz_cmd.command("move")
@click.option("--pan", "pan", type=int, default=0, help="Pan offset (signed; left negative).")
@click.option("--tilt", "tilt", type=int, default=0, help="Tilt offset (signed; down negative).")
@click.option("--zoom", "zoom", type=int, default=0, help="Zoom offset (signed; out negative).")
@click.pass_context
def ptz_move(ctx: click.Context, pan: int, tilt: int, zoom: int) -> None:
    """Issue a combined pan/tilt/zoom offset move (FR-14..16, no direction shorthand)."""
    _run_motion(
        ctx,
        action="move",
        direction=None,
        step=0,
        pan=pan,
        tilt=tilt,
        zoom=zoom,
    )


@ptz_cmd.command("stop")
@click.pass_context
def ptz_stop(ctx: click.Context) -> None:
    """Halt any in-progress motion (FR-17)."""
    _run_motion(ctx, action="stop", direction=None, step=0)


# ---------------------------------------------------------------------------
# Coroutine entry point
# ---------------------------------------------------------------------------


def _run_motion(
    ctx: click.Context,
    *,
    action: str,
    direction: str | None,
    step: int,
    pan: int = 0,
    tilt: int = 0,
    zoom: int = 0,
) -> None:
    state = ctx.obj
    target = state["__ptz_target__"]
    parent = ctx.parent
    parent_state = parent.obj if parent is not None else state
    mode: OutputMode = parent_state["mode"]
    timeout = float(parent_state.get("timeout") or 5.0)
    config_path = parent_state.get("config_path")
    credential_source = parent_state.get("credential_source")
    concurrency = parent_state.get("concurrency")

    rc = _run_async(
        lambda: _run(
            target=target,
            action=action,
            direction=direction,
            step=step,
            pan=pan,
            tilt=tilt,
            zoom=zoom,
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
    direction: str | None,
    step: int,
    pan: int,
    tilt: int,
    zoom: int,
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

    # Load the config so we can detect group targets BEFORE connecting.
    cfg, _ = load_config_with_target(target, config_path)

    # Group fan-out (FR-39..43): dispatch per-member if the target names
    # a configured group.
    if is_group_target(target, cfg):
        members = group_members(target, cfg)

        async def _per_target(alias: str) -> tuple[int, dict[str, object]]:
            record = await _execute_ptz(
                alias=alias,
                action=action,
                direction=direction,
                step=step,
                pan=pan,
                tilt=tilt,
                zoom=zoom,
                timeout=timeout,
                config_path=config_path,
                credential_source=credential_source,
            )
            return 0, record

        return await run_fanout(
            members=members,
            per_target=_per_target,
            concurrency=concurrency or cfg.defaults.concurrency,
            mode=mode,
        )

    record = await _execute_ptz(
        alias=target,
        action=action,
        direction=direction,
        step=step,
        pan=pan,
        tilt=tilt,
        zoom=zoom,
        timeout=timeout,
        config_path=config_path,
        credential_source=credential_source,
    )
    emit(record, mode, formatter=_to_text)
    return EXIT_SUCCESS


async def _execute_ptz(
    *,
    alias: str,
    action: str,
    direction: str | None,
    step: int,
    pan: int,
    tilt: int,
    zoom: int,
    timeout: float,
    config_path: object,
    credential_source: object,
) -> dict[str, object]:
    """Run one PTZ op on ``alias`` and return the record dict.

    Pulled out of :func:`_run` so the group-fan-out helper can reuse it
    per-member without the emit step.
    """
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

    needs_zoom = action == "zoom" or (action == "move" and zoom != 0)
    ptz_mode = require_ptz(
        model=model, target=conn.target.alias, require_zoom=needs_zoom
    )
    step_unit = "degrees" if ptz_mode == "continuous" else "device-step-units"

    started = time.monotonic()
    record: dict[str, object] = {
        "target": conn.target.alias,
        "action": action,
    }

    if action == "stop":
        await asyncio.to_thread(_call_stop, conn.tapo)
        record["stopped"] = True
    elif action == "pan":
        magnitude = step if direction == "right" else -step
        await asyncio.to_thread(conn.tapo.moveMotor, magnitude, 0)
        record["direction"] = direction
        record["step"] = step
        record["step_unit"] = step_unit
    elif action == "tilt":
        magnitude = step if direction == "up" else -step
        await asyncio.to_thread(conn.tapo.moveMotor, 0, magnitude)
        record["direction"] = direction
        record["step"] = step
        record["step_unit"] = step_unit
    elif action == "zoom":
        # Zoom is always device-step-units.
        signed = step if direction == "in" else -step
        await asyncio.to_thread(_call_zoom, conn.tapo, signed)
        record["direction"] = direction
        record["step"] = step
        record["step_unit"] = "device-step-units"
    else:  # move
        await asyncio.to_thread(conn.tapo.moveMotor, pan, tilt)
        if zoom != 0:
            await asyncio.to_thread(_call_zoom, conn.tapo, zoom)
        record["pan"] = pan
        record["tilt"] = tilt
        record["zoom"] = zoom
        record["step_unit"] = step_unit

    record["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return record


# ---------------------------------------------------------------------------
# pytapo bridges (kept thin so tests can patch by name)
# ---------------------------------------------------------------------------


def _call_stop(tapo: Any) -> None:
    """Best-effort motor halt.

    Pytapo at the pinned SHA exposes :meth:`Tapo.setMotorOff` on some
    firmware revisions. When unavailable, ``moveMotor(0, 0)`` is a
    no-op-equivalent cancellation — but C200 firmware rejects a literal
    zero-magnitude move with ``MOTOR_LOCKED_ROTOR`` ("max pan/tilt range
    reached, error_code -64304"). We swallow that specific error because
    it means "no motion to stop", which is exactly the state the verb
    promises to deliver.

    Either path is acceptable — the verb's contract is "halt any
    in-progress motion immediately", which on synchronous-pytapo firmware
    is already true by the time we get here.
    """
    fn = getattr(tapo, "setMotorOff", None)
    if callable(fn):
        fn()
        return
    try:
        tapo.moveMotor(0, 0)
    except Exception as exc:
        # C200 firmware emits MOTOR_LOCKED_ROTOR / -64304 on a zero-magnitude
        # move at the rotational endstop. That's a no-op-confirmed result,
        # not a device fault — surfacing exit 1 here would lie about state.
        msg = str(exc).upper()
        if "MOTOR_LOCKED_ROTOR" in msg or "-64304" in msg:
            logger.debug("stop: swallowing C200 zero-move endstop error: %s", exc)
            return
        raise


def _call_zoom(tapo: Any, magnitude: int) -> None:
    """Best-effort zoom step.

    Tapo C225 (the only zoom-capable model on the §3.3.1 matrix as of v1)
    exposes zoom via lens-switch profiles in the stream verb, not through
    a dedicated pytapo zoom method. When pytapo grows a ``zoomStep`` verb
    in a future SHA we wire it here. Until then, this function is a
    placeholder that calls ``moveMotor`` with magnitude in the third
    parameter slot if pytapo accepts it; otherwise it raises so the
    verb's exit-1 path fires with a structured error.
    """
    fn = getattr(tapo, "zoomStep", None) or getattr(tapo, "setZoom", None)
    if callable(fn):
        fn(magnitude)
        return
    # Fallback: pytapo doesn't expose a zoom verb on this SHA. Raise so
    # the device-error envelope surfaces a clear "no zoom verb" message
    # instead of silently no-op-ing.
    raise RuntimeError(
        "pytapo at the pinned SHA does not expose a zoom verb; "
        "zoom motion is unsupported in this CLI version."
    )


def _to_text(record: object) -> str:
    if not isinstance(record, dict):
        return str(record)
    parts = [f"{record.get('target', '-')}", f"action={record.get('action')}"]
    for key in ("direction", "step", "step_unit", "pan", "tilt", "zoom"):
        if key in record:
            parts.append(f"{key}={record[key]}")
    if "elapsed_ms" in record:
        parts.append(f"elapsed_ms={record['elapsed_ms']}")
    return "\t".join(parts)


__all__ = ["ptz_cmd"]
