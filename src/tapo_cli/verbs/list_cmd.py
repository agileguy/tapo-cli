"""``tapo-cli list`` (SRD §5.2, FR-6, FR-6a, FR-6b, FR-7, FR-8).

Default: emit one row per ``[devices.<alias>]`` entry from the active
config. ``--probe`` adds a ``online`` bool by issuing a TCP/443 connect
(no pytapo handshake — the cheapest possible liveness signal). Default
output mode is JSONL on a non-tty per FR-46; text mode prints a tab-
separated table.

Order: aliases as defined in config (insertion preserved).
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

import click

from tapo_cli.config import load_config
from tapo_cli.errors import EXIT_SUCCESS
from tapo_cli.output import OutputMode, emit_stream
from tapo_cli.runner import run_async as _run_async


async def _probe_alive(ip: str, *, timeout: float) -> bool:
    """Cheap TCP/443 liveness check. Returns ``False`` on any failure."""
    if not ip:
        return False
    try:
        fut = asyncio.open_connection(ip, 443)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
    except (TimeoutError, OSError):
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    del reader
    return True


def _row_to_text(row: object) -> str:
    """Tab-separated table row: alias, ip, mac, model, online."""
    assert isinstance(row, dict)
    online = row.get("online")
    online_text = "-" if online is None else ("y" if online else "n")
    return (
        f"{row.get('alias', '') or '-'}\t"
        f"{row.get('ip', '') or '-'}\t"
        f"{row.get('mac', '') or '-'}\t"
        f"{row.get('model', '') or '-'}\t"
        f"{online_text}"
    )


@click.command("list")
@click.option(
    "--probe",
    "probe",
    is_flag=True,
    default=False,
    help="Probe each device on TCP/443 and include online: bool (FR-6a).",
)
@click.option(
    "--online-only",
    "online_only",
    is_flag=True,
    default=False,
    help="Implies --probe; suppress entries that don't respond (FR-8).",
)
@click.pass_context
def list_cmd(ctx: click.Context, *, probe: bool, online_only: bool) -> None:
    """List configured device aliases."""
    state = ctx.obj
    mode: OutputMode = state["mode"]
    timeout = float(state.get("timeout") or 5.0)
    config_path = state.get("config_path")

    rc = _run_async(
        lambda: _run(
            mode=mode,
            probe=probe or online_only,
            online_only=online_only,
            timeout=timeout,
            config_path=config_path,
        ),
        mode=mode,
    )
    sys.exit(rc)


async def _run(
    *,
    mode: OutputMode,
    probe: bool,
    online_only: bool,
    timeout: float,
    config_path: object,
) -> int:
    from pathlib import Path

    cfg_path = Path(str(config_path)).expanduser() if config_path else None
    cfg = load_config(cfg_path)

    rows: list[dict[str, object]] = []
    aliases = list(cfg.devices.keys())  # insertion order preserved by Python dict
    for alias in aliases:
        entry = cfg.devices[alias]
        rows.append(
            {
                "alias": alias,
                "ip": entry.ip,
                "mac": entry.mac,
                "model": entry.model,
                "online": None,
            }
        )

    if probe:
        # Run probes concurrently — list output ordering is preserved by zip.
        results = await asyncio.gather(
            *(_probe_alive(str(r.get("ip") or ""), timeout=timeout) for r in rows)
        )
        for row, alive in zip(rows, results, strict=True):
            row["online"] = alive

    if online_only:
        rows = [r for r in rows if r.get("online")]

    emit_stream(rows, mode, formatter=_row_to_text)
    return EXIT_SUCCESS


__all__ = ["list_cmd"]
