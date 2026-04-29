"""``tapo-cli night-vision <target> auto|on|off|ir-only|status`` (FR-32).

Drives pytapo's ``setDayNightMode``/``getDayNightMode``. Pytapo at the
pinned SHA accepts only three values for non-child devices: ``"auto"``,
``"on"``, ``"off"``. There is no native ``ir-only`` enum on the device
side — pytapo treats ``"on"`` as "force IR night mode" and ``"off"`` as
"force colour day mode".

Mode mapping (FR-32):

================  =========================  ===========================
CLI sub-verb      Sent to pytapo             Reported back from device
================  =========================  ===========================
``auto``          ``setDayNightMode("auto")``  ``"auto"``
``on``            ``setDayNightMode("on")``    ``"on"``
``off``           ``setDayNightMode("off")``   ``"off"``
``ir-only``       ``setDayNightMode("on")``    ``"on"``  (alias)
``status``        — (read only)               whatever the device reports
================  =========================  ===========================

The CLI's mode-mapping carve-out: ``ir-only`` is a CLI-side affordance.
The C200 does not distinguish "always IR" from "always night-vision"; both
pin the IR cut filter open and engage the IR LEDs continuously. We surface
``ir-only`` as a sub-verb so the SRD's documented vocabulary works on
day-one, but it maps to the same wire payload as ``on``.

The status sub-verb passes through whatever pytapo emits — typically
``"auto"``, ``"on"``, or ``"off"``. We never lie about the device's actual
state (``"ir-only"`` is never reported back; that string only goes IN).

Output shape: ``{target, night_vision_mode: str}``.
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
from tapo_cli.verbs._target import load_config_with_target

logger = logging.getLogger("tapo_cli")

# Map CLI sub-verb → pytapo setDayNightMode argument.
# pytapo allows only {"off", "on", "auto"}; "ir-only" is a CLI alias for "on".
_MODE_TO_PYTAPO: dict[str, str] = {
    "auto": "auto",
    "on": "on",
    "off": "off",
    "ir-only": "on",
}


# ---------------------------------------------------------------------------
# Click verb
# ---------------------------------------------------------------------------


@click.command("night-vision")
@click.argument("target", type=str)
@click.argument(
    "action",
    type=click.Choice(["auto", "on", "off", "ir-only", "status"]),
)
@click.pass_context
def night_vision_cmd(ctx: click.Context, target: str, action: str) -> None:
    """Set the camera's night-vision mode, or report it."""
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
    from tapo_cli import wrapper as wrap

    cfg, resolved_target = load_config_with_target(target, config_path)
    conn = await wrap.connect(
        cfg,
        resolved_target,
        credential_source=credential_source,  # type: ignore[arg-type]
        timeout=timeout,
    )

    if action == "status":
        reported_mode = await asyncio.to_thread(_read_mode, conn.tapo)
    else:
        wire = _MODE_TO_PYTAPO[action]
        await asyncio.to_thread(conn.tapo.setDayNightMode, wire)
        # Echo back the *requested* sub-verb so tooling knows whether the
        # operator asked for ``ir-only`` (a CLI carve-out) vs ``on`` even
        # though both produce the same on-device state. This is the same
        # contract kasa-cli's color verbs honor.
        reported_mode = action

    record = {"target": conn.target.alias, "night_vision_mode": reported_mode}
    emit(record, mode, formatter=_to_text)
    return EXIT_SUCCESS


def _read_mode(tapo: Any) -> str:
    """Map pytapo's ``getDayNightMode`` return into our enum string."""
    raw: object = tapo.getDayNightMode()
    if isinstance(raw, str) and raw:
        return raw
    return "unknown"


def _to_text(record: object) -> str:
    if not isinstance(record, dict):
        return str(record)
    return f"{record.get('target', '-')}\tnight-vision={record.get('night_vision_mode')}"


__all__ = ["night_vision_cmd"]
