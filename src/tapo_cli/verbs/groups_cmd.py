"""``tapo-cli groups list`` (SRD §5.15, FR-39..43, FR-43b).

v1 ships only the read-only ``groups list`` sub-verb. Mutations are by
hand-editing the config file (FR-43b explicitly defers ``groups add`` /
``groups remove`` for v1 — same posture as kasa-cli).

Output shape per group::

    {"name": "<group>", "members": [{"alias": "<alias>", "ip": "<ip>"}, ...]}

Members are emitted in the order they appear in the TOML — :class:`Config`
preserves insertion order via the dict iteration order Python 3.7+
guarantees.

If the config has no ``[groups]`` table at all, ``groups list`` exits 0
with an empty array (text mode prints "no groups defined" on stderr so
operators don't think the command silently broke).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

from tapo_cli.config import Config, load_config
from tapo_cli.errors import EXIT_SUCCESS
from tapo_cli.output import OutputMode, emit, emit_stream
from tapo_cli.runner import run_async as _run_async

logger = logging.getLogger("tapo_cli")


# ---------------------------------------------------------------------------
# Click verb tree
# ---------------------------------------------------------------------------


@click.group("groups")
def groups_cmd() -> None:
    """Read-only ``groups list`` sub-verb (mutations by editing config; FR-43b)."""


@groups_cmd.command("list")
@click.pass_context
def groups_list(ctx: click.Context) -> None:
    """List every defined group with its member aliases (FR-39..43)."""
    state = ctx.obj
    mode: OutputMode = state["mode"]
    config_path = state.get("config_path")

    rc = _run_async(
        lambda: _run_list(mode=mode, config_path=config_path),
        mode=mode,
    )
    sys.exit(rc)


async def _run_list(*, mode: OutputMode, config_path: object) -> int:
    cfg_path = Path(str(config_path)).expanduser() if config_path else None
    cfg = load_config(cfg_path)
    items = _project_groups(cfg)

    if mode is OutputMode.JSON:
        emit(items, mode, formatter=lambda r: _row_text(r))
        return EXIT_SUCCESS
    if mode is OutputMode.TEXT:
        if not items:
            sys.stdout.write("no groups defined\n")
            return EXIT_SUCCESS
        for row in items:
            sys.stdout.write(_row_text(row) + "\n")
        return EXIT_SUCCESS
    if mode is OutputMode.QUIET:
        return EXIT_SUCCESS
    # JSONL: one row per group.
    emit_stream(items, mode, formatter=lambda r: _row_text(r))
    return EXIT_SUCCESS


def _project_groups(cfg: Config) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for group_name, members in cfg.groups.items():
        member_records: list[dict[str, str]] = []
        for alias in members:
            entry = cfg.devices.get(alias)
            ip = entry.ip if entry is not None and entry.ip else ""
            member_records.append({"alias": alias, "ip": ip})
        out.append({"name": group_name, "members": member_records})
    return out


def _row_text(record: object) -> str:
    if not isinstance(record, dict):
        return str(record)
    name = str(record.get("name", "-"))
    members = record.get("members") or []
    if isinstance(members, list):
        labels = ", ".join(
            str(m.get("alias", "?"))
            for m in members
            if isinstance(m, dict)
        )
    else:
        labels = ""
    return f"{name}: [{labels}]"


__all__ = ["groups_cmd"]
