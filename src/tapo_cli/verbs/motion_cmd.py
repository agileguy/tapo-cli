"""``tapo-cli motion <target> enable|disable|status|history`` (SRD §5.9).

Phase 3 promotes ``motion`` from a single command to a Click group with
four sub-verbs. The first three (``enable`` / ``disable`` / ``status``)
ship in Phase 1d; ``history`` is added here per FR-25..25d / B8.

Pytapo's ``setMotionDetection``/``getMotionDetection`` operate on the
``motion_detection.motion_det`` config table:

* ``setMotionDetection(enabled=bool, sensitivity=False, chn_id=None)``
* ``getMotionDetection()`` returns ``{"enabled": "on"|"off",
  "digital_sensitivity": "20"|"40"|"60"|"80", "sensitivity": "low"|...}``

For ``enable``/``disable`` we pass ``enabled=bool`` and let pytapo round-
trip the existing sensitivity. ``status`` reports the boolean plus the
device-reported sensitivity so dashboards can render both without
re-querying.

History (FR-25): ``motion history <target> [--since <RFC3339>] [--limit N]
[--event-type motion|doorbell-press]``. Results are sorted ascending by
``ts`` (FR-25c) and emitted as JSONL one per line by default. Future
``--since`` exits 0 with empty output (FR-25d). Pytapo's ``getEvents()``
returns epoch-second timestamps (already corrected for the camera-clock
offset); we project these to RFC 3339 UTC ``Z`` per §7.2.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import sys
from typing import Any

import click

from tapo_cli.errors import EXIT_SUCCESS, UsageError
from tapo_cli.output import OutputMode, emit, emit_stream, epoch_to_rfc3339
from tapo_cli.runner import run_async as _run_async
from tapo_cli.verbs._target import load_config_with_target

logger = logging.getLogger("tapo_cli")


# ---------------------------------------------------------------------------
# Click verb tree
# ---------------------------------------------------------------------------


class _MotionCommand(click.Command):
    """Custom command that dispatches between flat positional form and the
    ``history`` sub-verb based on the first argv token.

    ``motion history <target> [...]`` → forwards to :func:`motion_history`
    with all remaining argv. Anything else is the legacy
    ``motion <target> enable|disable|status|history`` form.
    """

    def __init__(self) -> None:
        super().__init__(
            name="motion",
            help=(
                "Motion detection: enable / disable / status / history.\n\n"
                "Forms:\n"
                "  motion <target> enable | disable | status | history\n"
                "  motion history <target> [--since RFC3339] "
                "[--limit N] [--event-type ...]"
            ),
            params=[],
        )

    def invoke(self, ctx: click.Context) -> None:
        # Default Click behaviour for a leaf command — we do all the work
        # in :meth:`parse_args`; nothing else to invoke.
        return None

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        # If first arg is ``history``, peel it and re-route through the
        # history sub-verb's Click parser to honour --since/--limit/etc.
        if args and args[0] == "history":
            from click import Context as _Ctx

            history_args = args[1:]
            with _Ctx(motion_history, info_name="history", parent=ctx) as sub_ctx:
                motion_history.parse_args(sub_ctx, list(history_args))
                ctx.invoked_subcommand = "history"
                ctx.exit(motion_history.invoke(sub_ctx) or 0)

        # Legacy flat form: exactly two tokens, target then action.
        if len(args) != 2:
            raise UsageError(
                "motion requires a target and an action",
                hint=(
                    "Examples: motion office status, "
                    "motion history office --limit 10."
                ),
            )
        target, action = args
        valid_actions = {"enable", "disable", "status", "history"}
        if action not in valid_actions:
            raise UsageError(
                f"unknown motion action: {action!r}",
                hint=f"One of: {', '.join(sorted(valid_actions))}",
            )

        ctx.obj = dict(ctx.obj or {})
        ctx.obj["__motion_target__"] = target

        if action == "history":
            _run_simple(ctx, action="history-default")
            return []
        _run_simple(ctx, action=action)
        return []


motion_cmd = _MotionCommand()


@click.command("history")
@click.argument("target", type=str, required=True)
@click.option(
    "--since",
    "since",
    type=str,
    default=None,
    help="RFC 3339 / ISO 8601 timestamp (UTC assumed if no offset).",
)
@click.option(
    "--limit",
    "limit",
    type=int,
    default=50,
    show_default=True,
    help="Maximum events to emit (after sort).",
)
@click.option(
    "--event-type",
    "event_type_filter",
    type=click.Choice(
        ["motion", "person", "vehicle", "doorbell-press", "unknown"]
    ),
    default=None,
    help="Filter to one event type only.",
)
@click.pass_context
def motion_history(
    ctx: click.Context,
    target: str,
    since: str | None,
    limit: int,
    event_type_filter: str | None,
) -> None:
    """Emit motion-event history sorted ascending by ``ts`` (FR-25)."""
    # State lives on the top-level click group (cli.py main); when
    # ``motion_history`` is invoked directly as a sub-command its parent
    # is the top-level group. When invoked through _MotionCommand's
    # delegation, the parent points at the motion-leaf ctx whose parent
    # is the top-level group — walk up if needed.
    grand_state: dict[str, object] = {}
    cur = ctx.parent
    while cur is not None:
        if isinstance(cur.obj, dict) and "mode" in cur.obj:
            grand_state = cur.obj
            break
        cur = cur.parent

    mode: OutputMode = grand_state.get("mode")  # type: ignore[assignment]
    timeout_val = grand_state.get("timeout") or 5.0
    timeout = float(timeout_val)  # type: ignore[arg-type]
    config_path = grand_state.get("config_path")
    credential_source = grand_state.get("credential_source")
    concurrency = grand_state.get("concurrency")

    rc = _run_async(
        lambda: _run_history(
            target=target,
            since=since,
            limit=limit,
            event_type_filter=event_type_filter,
            mode=mode,
            timeout=timeout,
            config_path=config_path,
            credential_source=credential_source,
            concurrency=concurrency,
        ),
        mode=mode,
    )
    sys.exit(rc)


# ---------------------------------------------------------------------------
# Helper: re-dispatch the flat positional form
# ---------------------------------------------------------------------------


def _run_simple(ctx: click.Context, *, action: str) -> None:
    state = ctx.obj
    target = state["__motion_target__"]
    parent = ctx.parent
    parent_state = parent.obj if parent is not None else state
    mode: OutputMode = parent_state["mode"]
    timeout = float(parent_state.get("timeout") or 5.0)
    config_path = parent_state.get("config_path")
    credential_source = parent_state.get("credential_source")
    concurrency = parent_state.get("concurrency")

    if action == "history-default":
        rc = _run_async(
            lambda: _run_history(
                target=target,
                since=None,
                limit=50,
                event_type_filter=None,
                mode=mode,
                timeout=timeout,
                config_path=config_path,
                credential_source=credential_source,
                concurrency=concurrency,
            ),
            mode=mode,
        )
        sys.exit(rc)

    rc = _run_async(
        lambda: _run_simple_async(
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


async def _run_simple_async(
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
            record = await _execute_motion_simple(
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

    record = await _execute_motion_simple(
        alias=target,
        action=action,
        config_path=config_path,
        credential_source=credential_source,
        timeout=timeout,
    )
    emit(record, mode, formatter=_to_text)
    return EXIT_SUCCESS


async def _execute_motion_simple(
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
    return record


# ---------------------------------------------------------------------------
# Coroutine: history (FR-25..25d, B8)
# ---------------------------------------------------------------------------


async def _run_history(
    *,
    target: str,
    since: str | None,
    limit: int,
    event_type_filter: str | None,
    mode: OutputMode,
    timeout: float,
    config_path: object,
    credential_source: object,
    concurrency: int | None = None,
) -> int:
    if limit <= 0:
        raise UsageError(
            f"--limit must be positive, got {limit}",
            hint="Pass a value >= 1.",
        )

    # Parse --since BEFORE we open a connection — bad input is a usage error
    # and shouldn't tie up the camera.
    since_epoch: float | None = None
    now_epoch = dt.datetime.now(tz=dt.UTC).timestamp()
    if since is not None:
        since_epoch = _parse_since(since)
        if since_epoch > now_epoch:
            # FR-25d: future --since → exit 0 with empty array.
            _emit_history(items=[], mode=mode)
            return EXIT_SUCCESS

    from tapo_cli.verbs._fanout import (
        group_members,
        is_group_target,
        run_fanout,
    )

    cfg, _ = load_config_with_target(target, config_path)
    if is_group_target(target, cfg):
        members = group_members(target, cfg)

        async def _per_target(alias: str) -> tuple[int, dict[str, object]]:
            items = await _execute_history(
                alias=alias,
                since_epoch=since_epoch,
                event_type_filter=event_type_filter,
                config_path=config_path,
                credential_source=credential_source,
                timeout=timeout,
                limit=limit,
                now_epoch=now_epoch,
            )
            return 0, {"target": alias, "events": items, "count": len(items)}

        return await run_fanout(
            members=members,
            per_target=_per_target,
            concurrency=concurrency or cfg.defaults.concurrency,
            mode=mode,
        )

    items = await _execute_history(
        alias=target,
        since_epoch=since_epoch,
        event_type_filter=event_type_filter,
        config_path=config_path,
        credential_source=credential_source,
        timeout=timeout,
        limit=limit,
        now_epoch=now_epoch,
    )
    _emit_history(items=items, mode=mode)
    return EXIT_SUCCESS


async def _execute_history(
    *,
    alias: str,
    since_epoch: float | None,
    event_type_filter: str | None,
    config_path: object,
    credential_source: object,
    timeout: float,
    limit: int,
    now_epoch: float,
) -> list[dict[str, object]]:
    from tapo_cli import wrapper as wrap

    cfg, resolved_target = load_config_with_target(alias, config_path)
    conn = await wrap.connect(
        cfg,
        resolved_target,
        credential_source=credential_source,  # type: ignore[arg-type]
        timeout=timeout,
    )

    default_window_seconds = 24 * 3600
    if since_epoch is not None:
        start_arg: float = since_epoch
    else:
        start_arg = now_epoch - default_window_seconds

    try:
        raw_events = await asyncio.to_thread(
            _call_get_events, conn.tapo, start_arg
        )
    except Exception as exc:
        msg = str(exc)
        if "-71112" in msg or "playback" in msg.lower():
            logger.info(
                "motion history: device reports no playback/SD-card index "
                "available (%s); emitting empty result",
                msg,
            )
            return []
        raise
    items = _project_events(
        raw_events,
        target_alias=conn.target.alias,
        since_epoch=since_epoch,
        event_type_filter=event_type_filter,
    )
    items.sort(key=lambda r: str(r.get("ts", "")))
    if limit > 0:
        items = items[:limit]
    return items


def _call_get_events(tapo: Any, start_time: float) -> list[Any]:
    """Call ``tapo.getEvents(startTime=...)`` and coerce result to a list.

    Pytapo's signature is ``getEvents(startTime=False, endTime=False)`` —
    a falsy ``startTime`` means "10 minutes ago"; we always pass an
    explicit epoch so the window matches our --since.
    """
    try:
        result = tapo.getEvents(startTime=start_time)
    except TypeError:
        # Older / alt-named variant — fall back to positional.
        result = tapo.getEvents(start_time)
    if isinstance(result, list):
        return result
    return []


def _project_events(
    raw_events: list[Any],
    *,
    target_alias: str,
    since_epoch: float | None,
    event_type_filter: str | None,
) -> list[dict[str, object]]:
    """Normalize pytapo events to the §10 motion-event shape (FR-25a).

    pytapo events look roughly like::

        {
          "start_time": <epoch>, "end_time": <epoch>,
          "startRelative": <int>, "endRelative": <int>,
          "type": <int|str>, "region": <int|str>,
          "video_id": <int>           # presence implies has_clip
        }

    We project to ``{target, ts, event_type, region, has_clip}``.
    """
    out: list[dict[str, object]] = []
    for ev in raw_events:
        if not isinstance(ev, dict):
            continue
        start = ev.get("start_time")
        if not isinstance(start, (int, float)):
            continue
        if since_epoch is not None and float(start) < since_epoch:
            continue
        event_type = _classify_event_type(ev)
        if event_type_filter and event_type != event_type_filter:
            continue
        region = ev.get("region", "full")
        if not isinstance(region, str):
            region = str(region)
        has_clip = bool(ev.get("video_id") or ev.get("has_clip"))
        out.append(
            {
                "target": target_alias,
                "ts": epoch_to_rfc3339(float(start)),
                "event_type": event_type,
                "region": region,
                "has_clip": has_clip,
            }
        )
    return out


def _classify_event_type(ev: dict[str, Any]) -> str:
    """Map pytapo's int/string ``type`` field to FR-25a's enum.

    pytapo's ``type`` is firmware-dependent. Accept either a numeric int
    or a string label; unknown values fall through to ``"unknown"`` so we
    don't lose the row.
    """
    raw = ev.get("type")
    if isinstance(raw, str):
        lowered = raw.lower()
        if "doorbell" in lowered or "ring" in lowered:
            return "doorbell-press"
        if "person" in lowered:
            return "person"
        if "vehicle" in lowered:
            return "vehicle"
        if "motion" in lowered:
            return "motion"
        return "unknown"
    if isinstance(raw, int):
        return {
            1: "motion",
            7: "person",
            9: "vehicle",
            11: "doorbell-press",
        }.get(raw, "unknown")
    # Some firmware emits no type field at all — assume motion since
    # getEvents() is the motion-events endpoint.
    return "motion"


def _emit_history(*, items: list[dict[str, object]], mode: OutputMode) -> None:
    if mode is OutputMode.JSON:
        # Single pretty JSON array.
        emit(items, mode, formatter=lambda r: _history_text(r))
        return
    if mode is OutputMode.TEXT:
        if not items:
            return
        sys.stdout.write("ts\ttype\tregion\thas_clip\n")
        for item in items:
            sys.stdout.write(_history_text(item) + "\n")
        return
    if mode is OutputMode.QUIET:
        return
    # JSONL: one event per line.
    emit_stream(items, mode, formatter=lambda r: _history_text(r))


def _history_text(record: object) -> str:
    if not isinstance(record, dict):
        return str(record)
    return "\t".join(
        [
            str(record.get("ts", "-")),
            str(record.get("event_type", "-")),
            str(record.get("region", "-")),
            str(record.get("has_clip", False)).lower(),
        ]
    )


# ---------------------------------------------------------------------------
# --since parser (FR-25b)
# ---------------------------------------------------------------------------


def _parse_since(value: str) -> float:
    """Parse a flexible RFC 3339 / ISO / shorthand string to epoch seconds.

    Accepted forms:

    * ``2026-04-28T08:14:02Z`` (canonical RFC 3339 UTC)
    * ``2026-04-28T08:14:02+02:00`` (RFC 3339 with offset)
    * ``2026-04-28T08:14:02`` (no offset → assume UTC, INFO log)
    * ``2026-04-28`` (bare date → 00:00:00Z, FR-25b)
    * ``1h``, ``30m``, ``7d``, ``60s`` (relative — convenience extension)
    """
    s = value.strip()
    if not s:
        raise UsageError(
            "--since may not be empty",
            hint=(
                "Pass an RFC 3339 timestamp (e.g., 2026-04-28T08:00:00Z) "
                "or a relative shorthand (1h / 30m / 7d)."
            ),
        )

    # Relative shorthand: 1h / 30m / 7d / 60s.
    if s[-1].lower() in ("s", "m", "h", "d") and s[:-1].isdigit():
        n = int(s[:-1])
        unit = s[-1].lower()
        seconds = {
            "s": n,
            "m": n * 60,
            "h": n * 3600,
            "d": n * 86400,
        }[unit]
        return dt.datetime.now(tz=dt.UTC).timestamp() - seconds

    # Bare date (FR-25b): treat as 00:00:00Z.
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        try:
            d = dt.date.fromisoformat(s)
        except ValueError as exc:
            raise UsageError(
                f"--since {value!r} is not a valid date",
                hint="Use YYYY-MM-DD or a full RFC 3339 timestamp.",
            ) from exc
        return dt.datetime(
            d.year, d.month, d.day, tzinfo=dt.UTC
        ).timestamp()

    # RFC 3339 / ISO 8601. ``fromisoformat`` accepts ``Z`` only on
    # 3.11.4+; swap to ``+00:00`` to be safe.
    iso = s.replace("Z", "+00:00") if s.endswith("Z") else s
    try:
        parsed = dt.datetime.fromisoformat(iso)
    except ValueError as exc:
        raise UsageError(
            f"--since {value!r} is not a valid RFC 3339 timestamp",
            hint="Examples: 2026-04-28T08:00:00Z, 2026-04-28, 1h.",
        ) from exc

    if parsed.tzinfo is None:
        # FR-25b: no offset → assume UTC + emit INFO log line on stderr.
        logger.info("--since %r has no offset; assuming UTC", value)
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.timestamp()


# ---------------------------------------------------------------------------
# enable/disable/status helpers (carried from Phase 1d)
# ---------------------------------------------------------------------------


def _read_motion_state(tapo: Any) -> tuple[bool, str | None]:
    """Return ``(enabled, sensitivity)`` from pytapo's getMotionDetection.

    Sensitivity is best-effort — older firmware emits ``digital_sensitivity``
    (a numeric string), newer firmware emits both ``digital_sensitivity``
    and ``sensitivity`` (a string label). Prefer the label; fall back to
    the numeric. ``None`` means the field wasn't present.
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
