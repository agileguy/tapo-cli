"""``tapo-cli reboot <target>`` (FR-38, S13).

Confirmation contract (FR-38):

* tty mode without ``--yes`` → prompt on **stderr** (so stdout JSON/text
  contracts survive when the operator pipes the output) for ``y``/``N``;
  default is N (no reboot, exit 0 with no action emitted).
* tty or non-tty with ``--yes`` → proceed without prompting.
* non-tty without ``--yes`` → exit 64 (usage error). Pipelines must opt
  in explicitly.
* ``--quiet`` (which is the global ``--quiet`` flag) implies ``--yes``.
  A quiet caller has signalled "no UI, just do it"; the alternative —
  failing silently for missing ``--yes`` — is worse than a clear contract.

After the reboot RPC succeeds we emit ``{target, status: "reboot-issued"}``
and return 0. We deliberately do NOT wait for the camera to come back —
that's the operator's job (and pytapo's reboot RPC blocks the device for
30-60 seconds anyway, which would blow our 5s default timeout).

Pytapo at the pinned SHA exposes ``reboot(delay=None)``; legacy firmware
ignores the delay and takes ~30s, KLAP firmware honours a 1-second delay.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

import click

from tapo_cli.errors import EXIT_SUCCESS, UsageError
from tapo_cli.output import OutputMode, emit
from tapo_cli.runner import run_async as _run_async
from tapo_cli.verbs._target import load_config_with_target

logger = logging.getLogger("tapo_cli")


# ---------------------------------------------------------------------------
# Click verb
# ---------------------------------------------------------------------------


@click.command("reboot")
@click.argument("target", type=str)
@click.option(
    "--yes",
    "-y",
    "yes_flag",
    is_flag=True,
    default=False,
    help="Skip the interactive confirmation prompt (implied by --quiet).",
)
@click.pass_context
def reboot_cmd(ctx: click.Context, target: str, yes_flag: bool) -> None:
    """Reboot the camera (or every member of an @group).

    FR-38 single-target confirmation: tty mode prompts on stderr; non-tty
    requires ``--yes``. ``--quiet`` implies ``--yes``.

    FR-43e group confirmation: ``reboot @group`` applies the FR-38 rules
    at the group level — one prompt naming the resolved member list, NOT
    one prompt per camera. ``--yes`` / ``--quiet`` short-circuit the
    prompt; the per-camera fan-out then proceeds with no further prompts.
    """
    state = ctx.obj
    mode: OutputMode = state["mode"]
    timeout = float(state.get("timeout") or 5.0)
    config_path = state.get("config_path")
    credential_source = state.get("credential_source")
    concurrency = state.get("concurrency")

    # FR-38 / FR-43e: --quiet implies --yes.
    yes_effective = yes_flag or (mode is OutputMode.QUIET)

    rc = _run_async(
        lambda: _confirm_then_run(
            target=target,
            yes=yes_effective,
            mode=mode,
            timeout=timeout,
            config_path=config_path,
            credential_source=credential_source,
            concurrency=concurrency,
        ),
        mode=mode,
    )
    sys.exit(rc)


async def _confirm_then_run(
    *,
    target: str,
    yes: bool,
    mode: OutputMode,
    timeout: float,
    config_path: object,
    credential_source: object,
    concurrency: int | None = None,
) -> int:
    """Run the confirmation gate, then dispatch to the reboot RPC.

    FR-43e: detect ``@group`` BEFORE prompting so the prompt names the
    group members. Then fan out (no per-camera prompts).
    """
    from tapo_cli.verbs._fanout import (
        group_members,
        is_group_target,
        run_fanout,
    )

    cfg, _ = load_config_with_target(target, config_path)

    if is_group_target(target, cfg):
        members = group_members(target, cfg)
        if not _confirm_or_fail_group(yes=yes, target=target, members=members):
            return EXIT_SUCCESS

        async def _per_target(alias: str) -> tuple[int, dict[str, object]]:
            record = await _execute_reboot(
                alias=alias,
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

    # Single-target path (unchanged FR-38 contract).
    confirmed = _confirm_or_fail(yes=yes)
    if not confirmed:
        return EXIT_SUCCESS

    return await _run(
        target=target,
        mode=mode,
        timeout=timeout,
        config_path=config_path,
        credential_source=credential_source,
    )


def _confirm_or_fail_group(
    *, yes: bool, target: str, members: list[str]
) -> bool:
    """FR-43e group-level confirmation gate. ``--yes`` skips; tty prompts; non-tty exits 64.

    The prompt names the group, enumerates the resolved member aliases on
    stderr, and reads y/N. Anything other than y / yes is treated as a
    decline (returns False).
    """
    if yes:
        return True

    if not _is_interactive_tty():
        raise UsageError(
            f"reboot {target!r} requires --yes when stdin/stderr is not a tty",
            hint=(
                "Group reboot is destructive across multiple cameras. Pass "
                "--yes (or --quiet, which implies --yes) to confirm."
            ),
        )

    member_list = ", ".join(members) if members else "(empty)"
    sys.stderr.write(
        f"About to reboot {len(members)} camera(s) in group "
        f"{target.lstrip('@') or target!r}: {member_list}. Reboot? [y/N] "
    )
    sys.stderr.flush()
    try:
        response = sys.stdin.readline()
    except (EOFError, KeyboardInterrupt):
        return False
    return response.strip().lower() in {"y", "yes"}


async def _execute_reboot(
    *,
    alias: str,
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
    await asyncio.to_thread(_invoke_reboot, conn.tapo)
    return {"target": conn.target.alias, "status": "reboot-issued"}


def _is_interactive_tty() -> bool:
    """Module-level seam: ``True`` iff stdin AND stderr both look like ttys.

    Hoisted to its own function so tests can monkeypatch it without having
    to wrestle Click's ``CliRunner``-patched ``sys.stdin``/``sys.stderr``.
    """
    return bool(
        getattr(sys.stdin, "isatty", lambda: False)()
        and getattr(sys.stderr, "isatty", lambda: False)()
    )


def _confirm_or_fail(*, yes: bool) -> bool:
    """Run the FR-38 confirmation gate.

    Returns ``True`` if the reboot may proceed, ``False`` if the operator
    declined the prompt, raises :class:`UsageError` for the non-tty-no-yes
    path (exit 64).
    """
    if yes:
        return True

    # Stderr is what FR-38 mandates for the prompt — preserves stdout
    # JSON/text contracts for piped consumers.
    if not _is_interactive_tty():
        raise UsageError(
            "reboot requires --yes when stdin/stderr is not a tty",
            hint="Pass --yes (or --quiet, which implies --yes) to confirm.",
        )

    # Click's confirm uses stdout for the prompt; we want stderr per FR-38.
    sys.stderr.write("Reboot the camera? [y/N] ")
    sys.stderr.flush()
    try:
        response = sys.stdin.readline()
    except (EOFError, KeyboardInterrupt):
        return False
    answer = response.strip().lower()
    return answer in {"y", "yes"}


async def _run(
    *,
    target: str,
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

    # Fire-and-forget. pytapo.reboot blocks while the camera ACKs; we let
    # it block for our timeout window only — anything beyond is reported
    # as success since the RPC already left our hands.
    await asyncio.to_thread(_invoke_reboot, conn.tapo)

    record = {"target": conn.target.alias, "status": "reboot-issued"}
    emit(record, mode, formatter=_to_text)
    return EXIT_SUCCESS


def _invoke_reboot(tapo: Any) -> None:
    """Call pytapo.reboot() ignoring its return value.

    Pytapo's reboot returns whatever the device sends back (legacy:
    ``{"error_code": 0}``; KLAP: an opaque dict). We don't need either.
    """
    tapo.reboot()


def _to_text(record: object) -> str:
    if not isinstance(record, dict):
        return str(record)
    return f"{record.get('target', '-')}\t{record.get('status')}"


__all__ = ["reboot_cmd"]
