"""``tapo-cli led <target> on|off|status`` (SRD §5.x).

Drives pytapo's ``setLEDEnabled``/``getLED`` against a single target.
The status indicator LED on the C200 is the small white LED visible
through the front bezel; toggling it is purely cosmetic but useful for
"is this thing actually targeting the right camera?" debugging.

Pytapo branches internally on KLAP vs legacy:

* legacy: ``setLedStatus`` with ``{"enabled": "on"|"off"}``
* KLAP:   ``set_led_off`` with ``{"led_off": 0|1}``

We don't care which path it takes — we just send a ``bool``. The
``getLED()`` accessor returns ``{"enabled": "on"|"off"}`` regardless.

Output shape: ``{target, led_enabled: bool}``.
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


# ---------------------------------------------------------------------------
# Click verb
# ---------------------------------------------------------------------------


@click.command("led")
@click.argument("target", type=str)
@click.argument("action", type=click.Choice(["on", "off", "status"]))
@click.pass_context
def led_cmd(ctx: click.Context, target: str, action: str) -> None:
    """Turn the camera status LED on or off, or report its state."""
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

    if action == "on":
        await asyncio.to_thread(conn.tapo.setLEDEnabled, True)
        led_enabled = True
    elif action == "off":
        await asyncio.to_thread(conn.tapo.setLEDEnabled, False)
        led_enabled = False
    else:  # status
        led_enabled = await asyncio.to_thread(_read_led_state, conn.tapo)

    record = {"target": conn.target.alias, "led_enabled": led_enabled}
    emit(record, mode, formatter=_to_text)
    return EXIT_SUCCESS


def _read_led_state(tapo: Any) -> bool:
    """Map pytapo's ``getLED`` shape to a bool.

    Legacy firmware: ``{"enabled": "on"|"off"}``.
    KLAP: pytapo normalizes through the same getter — it still surfaces
    ``enabled`` here. We accept either string or bool defensively.
    """
    raw: object = tapo.getLED()
    if isinstance(raw, dict):
        value = raw.get("enabled")
        if isinstance(value, str):
            return value.lower() == "on"
        if isinstance(value, bool):
            return value
    return False


def _to_text(record: object) -> str:
    if not isinstance(record, dict):
        return str(record)
    return f"{record.get('target', '-')}\tled={record.get('led_enabled')}"


__all__ = ["led_cmd"]
