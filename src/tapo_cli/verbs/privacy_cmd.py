"""``tapo-cli privacy <target> enable|disable|status`` (SRD §5.x, FR-31).

Drives pytapo's ``setPrivacyMode``/``getPrivacyMode`` against a single
target (alias, ``@alias``, or bare IPv4). The C200 reference camera
implements privacy as a lens-cover/lens-mask — when ``enabled`` the
camera shutter physically rotates closed, blanking the optical sensor
on-device.

Pytapo at the pinned SHA exposes:

* ``setPrivacyMode(enabled: bool)`` — internally calls
  ``setLensMaskConfig`` with ``{"enabled": "on"|"off"}``.
* ``getPrivacyMode()`` — returns ``{"enabled": "on"|"off"}``.

Output shape (FR-CRED-8 control-plane verb): ``{target, privacy_enabled: bool}``.
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


@click.command("privacy")
@click.argument("target", type=str)
@click.argument("action", type=click.Choice(["enable", "disable", "status"]))
@click.pass_context
def privacy_cmd(ctx: click.Context, target: str, action: str) -> None:
    """Enable, disable, or report the privacy lens-cover state.

    Accepts a single ``<target>`` (alias or bare IP) or an ``@group``
    target — the latter fans out to every group member with FR-43a
    exit-code semantics and one B10 envelope per camera (FR-43d).
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

    # FR-43d / FR-56: group fan-out via _fanout.run_fanout.
    if is_group_target(target, cfg):
        members = group_members(target, cfg)

        async def _per_target(alias: str) -> tuple[int, dict[str, object]]:
            record = await _execute_privacy(
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

    record = await _execute_privacy(
        alias=target,
        action=action,
        config_path=config_path,
        credential_source=credential_source,
        timeout=timeout,
    )
    emit(record, mode, formatter=_to_text)
    return EXIT_SUCCESS


async def _execute_privacy(
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

    if action == "enable":
        await asyncio.to_thread(conn.tapo.setPrivacyMode, True)
        privacy_enabled = True
    elif action == "disable":
        await asyncio.to_thread(conn.tapo.setPrivacyMode, False)
        privacy_enabled = False
    else:  # status
        privacy_enabled = await asyncio.to_thread(_read_privacy_state, conn.tapo)

    return {"target": conn.target.alias, "privacy_enabled": privacy_enabled}


def _read_privacy_state(tapo: Any) -> bool:
    """Map pytapo's ``{"enabled": "on"|"off"}`` payload to a bool."""
    raw: object = tapo.getPrivacyMode()
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
    return f"{record.get('target', '-')}\tprivacy={record.get('privacy_enabled')}"


__all__ = ["privacy_cmd"]
