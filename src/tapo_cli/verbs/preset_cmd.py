"""``tapo-cli preset <target> ...`` (FR-18..21).

Four sub-verbs covering the camera's saved-preset registry:

* ``preset list`` — emit ``[{id, name}]`` per saved position
* ``preset goto <name>`` — move to the named preset (resolves name to id)
* ``preset save <name>`` — save the current position under ``<name>``
* ``preset delete <name>`` — delete the named preset (resolves name to id)

Capability gate: presets are only on PTZ-capable models (the §3.3.1 matrix
flips ``preset: yes`` only on the same family as ``ptz_mode != none``).
We consult :func:`require_feature` with ``preset`` and exit 5 on
non-PTZ models with a hint listing supported model prefixes.

pytapo signatures verified at the pinned SHA (de5ca37):

* ``getPresets()`` — returns ``{<id-str>: <name-str>}`` on success.
* ``setPreset(presetID, retry=False)`` — go to preset by id.
* ``savePreset(name)`` — save the current position. The pytapo SHA does
  NOT expose an "overwrite by name" path; ``savePreset`` always creates a
  new entry. On overlapping name, we surface the new id and emit a WARN
  log line so callers can dedupe themselves.
* ``deletePreset(presetID, retry=False)`` — remove preset by id.

Name → id resolution uses :meth:`getPresets` and reverse-maps. Unknown
names exit code 4 (``not_found``) per FR-19/FR-21.

JSON output shapes:

* ``list``  : ``[{"id": <int>, "name": <str>}, ...]``
* ``goto``  : ``{"target", "action": "goto", "preset_id", "name"}``
* ``save``  : ``{"target", "action": "save", "preset_id", "name"}``
* ``delete``: ``{"target", "action": "delete", "preset_id", "name"}``
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

import click

from tapo_cli.errors import EXIT_SUCCESS, NotFoundError
from tapo_cli.output import OutputMode, emit, emit_stream
from tapo_cli.runner import run_async as _run_async
from tapo_cli.verbs._capability import require_feature, resolve_model_for_target
from tapo_cli.verbs._target import load_config_with_target

logger = logging.getLogger("tapo_cli")


# ---------------------------------------------------------------------------
# Click verb tree
# ---------------------------------------------------------------------------


@click.group("preset")
@click.argument("target", type=str)
@click.pass_context
def preset_cmd(ctx: click.Context, target: str) -> None:
    """List, goto, save, or delete saved camera positions (FR-18..21)."""
    ctx.obj = dict(ctx.obj or {})
    ctx.obj["__preset_target__"] = target


@preset_cmd.command("list")
@click.pass_context
def preset_list(ctx: click.Context) -> None:
    """List saved presets on the device (FR-18)."""
    _dispatch(ctx, action="list", name=None)


@preset_cmd.command("goto")
@click.argument("name", type=str)
@click.pass_context
def preset_goto(ctx: click.Context, name: str) -> None:
    """Move the camera to the named preset (FR-19). Unknown name → exit 4."""
    _dispatch(ctx, action="goto", name=name)


@preset_cmd.command("save")
@click.argument("name", type=str)
@click.pass_context
def preset_save(ctx: click.Context, name: str) -> None:
    """Save the current camera position as a preset (FR-20)."""
    _dispatch(ctx, action="save", name=name)


@preset_cmd.command("delete")
@click.argument("name", type=str)
@click.pass_context
def preset_delete(ctx: click.Context, name: str) -> None:
    """Delete the named preset (FR-21). Unknown name → exit 4."""
    _dispatch(ctx, action="delete", name=name)


# ---------------------------------------------------------------------------
# Coroutine dispatch
# ---------------------------------------------------------------------------


def _dispatch(ctx: click.Context, *, action: str, name: str | None) -> None:
    state = ctx.obj
    target = state["__preset_target__"]
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
            name=name,
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
    name: str | None,
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
            record = await _execute_preset(
                alias=alias,
                action=action,
                name=name,
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

    return await _execute_preset_emit(
        alias=target,
        action=action,
        name=name,
        config_path=config_path,
        credential_source=credential_source,
        timeout=timeout,
        mode=mode,
    )


async def _execute_preset_emit(
    *,
    alias: str,
    action: str,
    name: str | None,
    config_path: object,
    credential_source: object,
    timeout: float,
    mode: OutputMode,
) -> int:
    """Single-target path that streams the ``list`` shape via ``emit_stream``."""
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

    require_feature(
        model=model,
        target=conn.target.alias,
        feature="preset",
        verb_name="preset",
    )

    canonical_alias = conn.target.alias
    if action == "list":
        presets = await asyncio.to_thread(_load_presets, conn.tapo)
        records = [{"id": p[0], "name": p[1]} for p in presets]
        emit_stream(records, mode, formatter=_list_formatter)
        return EXIT_SUCCESS

    record = await _execute_preset_action(
        tapo=conn.tapo, alias=canonical_alias, action=action, name=name
    )
    emit(record, mode, formatter=_one_formatter)
    return EXIT_SUCCESS


async def _execute_preset(
    *,
    alias: str,
    action: str,
    name: str | None,
    config_path: object,
    credential_source: object,
    timeout: float,
) -> dict[str, object]:
    """Per-target preset execution for fan-out. Returns the JSON record.

    For ``action == "list"`` returns ``{"target", "action": "list", "presets": [...]}``
    so the B10 fan-out envelope can wrap it cleanly (single-target ``list``
    keeps using ``emit_stream`` for back-compat).
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
    require_feature(
        model=model,
        target=conn.target.alias,
        feature="preset",
        verb_name="preset",
    )

    canonical_alias = conn.target.alias
    if action == "list":
        presets = await asyncio.to_thread(_load_presets, conn.tapo)
        return {
            "target": canonical_alias,
            "action": "list",
            "presets": [{"id": p[0], "name": p[1]} for p in presets],
        }

    return await _execute_preset_action(
        tapo=conn.tapo, alias=canonical_alias, action=action, name=name
    )


async def _execute_preset_action(
    *,
    tapo: Any,
    alias: str,
    action: str,
    name: str | None,
) -> dict[str, object]:
    """Run goto / save / delete and return the JSON record."""
    assert name is not None  # click guarantees this

    if action == "save":
        new_id = await asyncio.to_thread(_save_preset, tapo, name)
        return {
            "target": alias,
            "action": "save",
            "preset_id": new_id,
            "name": name,
        }

    presets = await asyncio.to_thread(_load_presets, tapo)
    preset_id = _resolve_preset_id(presets, name)
    if preset_id is None:
        raise NotFoundError(
            f"unknown preset name: {name!r}",
            target=alias,
            hint=(
                "Run `tapo-cli preset <target> list` to see saved preset names. "
                "Names are matched case-insensitively."
            ),
        )

    if action == "goto":
        await asyncio.to_thread(tapo.setPreset, preset_id)
    else:  # delete
        await asyncio.to_thread(tapo.deletePreset, preset_id)

    return {
        "target": alias,
        "action": action,
        "preset_id": int(preset_id) if str(preset_id).isdigit() else preset_id,
        "name": name,
    }


# ---------------------------------------------------------------------------
# pytapo bridges
# ---------------------------------------------------------------------------


def _load_presets(tapo: Any) -> list[tuple[int | str, str]]:
    """Return preset (id, name) pairs in id-ascending order.

    pytapo's ``getPresets`` returns a dict keyed by preset id (sometimes
    string, sometimes int). We tolerate both, sort numerically when ids
    are integer-coercible, and fall back to lexicographic otherwise.
    """
    raw: object = tapo.getPresets()
    if not isinstance(raw, dict):
        return []
    pairs: list[tuple[int | str, str]] = []
    for key, value in raw.items():
        name = str(value) if value is not None else ""
        if isinstance(key, int):
            pairs.append((key, name))
        elif isinstance(key, str):
            try:
                pairs.append((int(key), name))
            except ValueError:
                pairs.append((key, name))
        else:
            pairs.append((str(key), name))

    def _sort_key(p: tuple[int | str, str]) -> tuple[int, object]:
        # Group ints first (group 0), strings after (group 1).
        if isinstance(p[0], int):
            return (0, p[0])
        return (1, p[0])

    pairs.sort(key=_sort_key)
    return pairs


def _save_preset(tapo: Any, name: str) -> int | str:
    """Save the current position as ``name`` and return the new preset id.

    pytapo's ``savePreset(name)`` returns the device's response. The shape
    varies by firmware — sometimes it's the new id, sometimes it's a
    confirmation envelope. We re-read the preset list afterward to find
    the entry whose name matches and surface its id.
    """
    tapo.savePreset(name)
    presets = _load_presets(tapo)
    matches = [pid for pid, pname in presets if pname == name]
    if matches:
        return matches[-1]  # newest entry on overlapping names
    if presets:
        # If we couldn't match the name (firmware that case-mangles, etc.),
        # surface the highest id as best-effort.
        return presets[-1][0]
    return -1


def _resolve_preset_id(
    presets: list[tuple[int | str, str]],
    name: str,
) -> int | str | None:
    """Reverse-map preset name → id with case-insensitive matching."""
    target_lower = name.lower()
    for pid, pname in presets:
        if pname.lower() == target_lower:
            return pid
    return None


# ---------------------------------------------------------------------------
# Text formatters
# ---------------------------------------------------------------------------


def _list_formatter(item: object) -> str:
    if not isinstance(item, dict):
        return str(item)
    return f"{item.get('id', '-')}\t{item.get('name', '')}"


def _one_formatter(record: object) -> str:
    if not isinstance(record, dict):
        return str(record)
    parts = [
        f"{record.get('target', '-')}",
        f"action={record.get('action')}",
        f"preset_id={record.get('preset_id')}",
        f"name={record.get('name')}",
    ]
    return "\t".join(parts)


__all__ = ["preset_cmd"]
