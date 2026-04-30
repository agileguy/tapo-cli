"""``tapo-cli motion <target> enable|disable|status|history`` (SRD §5.9)
plus ``motion download-clip`` (Phase 4c, FR-63..65, §16.4.3).

Phase 3 promotes ``motion`` from a single command to a Click group with
four sub-verbs. The first three (``enable`` / ``disable`` / ``status``)
ship in Phase 1d; ``history`` is added there per FR-25..25d / B8.
Phase 4c adds ``download-clip`` behind the mandatory
``--experimental-clips`` opt-in flag (FR-63).

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

Phase 4c (FR-63..65, §16.4.3): ``motion download-clip <target>
<event-id> --output PATH --experimental-clips``. The ``<event-id>`` is
the stable composite ``"<start_time>-<end_time>"`` string surfaced as the
``event_id`` field in motion history records (FR-63a). The download
backing path is pytapo's ``experiments/DownloadRecordings.py`` flow,
which in the installed pytapo package translates to
``pytapo.media_stream.downloader.Downloader`` (the experiments scripts
are reference call-sites for that class, not a separately importable
module). The wrapper hides the segment-list-then-ffmpeg dance behind a
single ``download_clip`` helper. ffmpeg is required on PATH (FR-64);
missing → exit 6. Devices without an SD card / event with
``has_clip: false`` → exit 4 with a structured hint distinguishing the
two cases (FR-64a). Output schema (FR-65) is
``{target, event_id, output_path, bytes, duration_s, mechanism}``
where ``mechanism`` is the deliberate observability flag
``"pytapo-experiments"`` so operators can detect upstream churn.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import shutil
import sys
import time
from typing import Any

import click

from tapo_cli.errors import (
    EXIT_SUCCESS,
    ConfigError,
    NetworkError,
    NotFoundError,
    UnsupportedFeatureError,
    UsageError,
)
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
                "Motion detection: enable / disable / status / history / "
                "download-clip.\n\n"
                "Forms:\n"
                "  motion <target> enable | disable | status | history\n"
                "  motion history <target> [--since RFC3339] "
                "[--limit N] [--event-type ...]\n"
                "  motion download-clip <target> <event-id> "
                "--output PATH --experimental-clips"
            ),
            params=[],
        )

    def invoke(self, ctx: click.Context) -> None:
        # Default Click behaviour for a leaf command — we do all the work
        # in :meth:`parse_args`; nothing else to invoke.
        return None

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        # ``motion --help`` (no positional yet) — emit the multi-form help
        # text so operators discover the sub-verbs without having to dig
        # into the SRD. We mirror Click's default help-flag behaviour
        # because :class:`_MotionCommand` has no registered params.
        if args and args[0] in ("--help", "-h"):
            click.echo(ctx.get_help())
            ctx.exit(0)

        # If first arg is ``history``, peel it and re-route through the
        # history sub-verb's Click parser to honour --since/--limit/etc.
        if args and args[0] == "history":
            from click import Context as _Ctx

            history_args = args[1:]
            with _Ctx(motion_history, info_name="history", parent=ctx) as sub_ctx:
                motion_history.parse_args(sub_ctx, list(history_args))
                ctx.invoked_subcommand = "history"
                ctx.exit(motion_history.invoke(sub_ctx) or 0)

        # Phase 4c (FR-63..65): download-clip sub-verb.
        if args and args[0] == "download-clip":
            from click import Context as _Ctx

            dl_args = args[1:]
            with _Ctx(
                motion_download_clip, info_name="download-clip", parent=ctx
            ) as sub_ctx:
                motion_download_clip.parse_args(sub_ctx, list(dl_args))
                ctx.invoked_subcommand = "download-clip"
                ctx.exit(motion_download_clip.invoke(sub_ctx) or 0)

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
    concurrency_raw = grand_state.get("concurrency")
    concurrency: int | None = (
        concurrency_raw if isinstance(concurrency_raw, int) else None
    )

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
# Phase 4c — download-clip (FR-63..65, §16.4.3)
# ---------------------------------------------------------------------------


@click.command("download-clip")
@click.argument("target", type=str, required=True)
@click.argument("event_id", type=str, required=True)
@click.option(
    "--output",
    "output",
    type=click.Path(dir_okay=False, writable=True),
    required=False,
    default=None,
    help="Destination MP4 path. Required when --experimental-clips is set.",
)
@click.option(
    "--experimental-clips",
    "experimental",
    is_flag=True,
    default=False,
    help=(
        "Required opt-in flag. The clip-download path is experimental "
        "and may break across firmware updates (SRD §16.4.3, FR-63)."
    ),
)
@click.pass_context
def motion_download_clip(
    ctx: click.Context,
    target: str,
    event_id: str,
    output: str | None,
    experimental: bool,
) -> None:
    """Download an SD-card recording for a motion event (FR-63..65)."""
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
    quiet_flag = bool(grand_state.get("quiet_flag"))

    # All validation lives inside the async runner so :class:`UsageError`
    # / other :class:`TapoCliError` subclasses route through
    # :func:`run_async`'s TapoCliError → exit-code mapping. Doing the
    # checks out here in the Click callback would bypass that mapping
    # and exit 1 instead of 64.
    rc = _run_async(
        lambda: _run_download_clip(
            target=target,
            event_id=event_id,
            output_path=output,
            experimental=experimental,
            quiet_flag=quiet_flag,
            mode=mode,
            timeout=timeout,
            config_path=config_path,
            credential_source=credential_source,
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
    """Normalize pytapo events to the §10 motion-event shape (FR-25a, FR-63a).

    pytapo events look roughly like::

        {
          "start_time": <epoch>, "end_time": <epoch>,
          "startRelative": <int>, "endRelative": <int>,
          "type": <int|str>, "region": <int|str>,
          "video_id": <int>           # presence implies has_clip
        }

    We project to ``{target, ts, event_type, region, has_clip, event_id}``.

    FR-63a: ``event_id`` is the stable composite
    ``"<start_time>-<end_time>"`` (epoch seconds, integer). pytapo's raw
    events do NOT carry an opaque event identifier — the start/end pair
    is the natural primary key the on-camera SD-card index keys on, and
    is the lookup key the ``Downloader`` class consumes (it takes
    ``startTime`` + ``endTime`` integers, not an opaque token). The
    composite form lets operators slice it back apart if they need to
    log / reason about the underlying timestamps without parsing the
    pretty ``ts`` field.
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
        end = ev.get("end_time")
        # FR-63a: stable composite event_id. ``end_time`` may be missing
        # on truly malformed events; we still emit the start-only form so
        # the field is always present (operators can still pass it to
        # download-clip — it'll exit 4 if the device can't resolve it).
        if isinstance(end, (int, float)):
            event_id = f"{int(start)}-{int(end)}"
        else:
            event_id = f"{int(start)}-{int(start)}"
        out.append(
            {
                "target": target_alias,
                "ts": epoch_to_rfc3339(float(start)),
                "event_type": event_type,
                "region": region,
                "has_clip": has_clip,
                "event_id": event_id,
            }
        )
    return out


def parse_event_id(event_id: str) -> tuple[int, int]:
    """Split a composite ``"<start>-<end>"`` event id into ints (FR-63a).

    Raises :class:`UsageError` on malformed input so callers get a clean
    exit-64 instead of a Python traceback.
    """
    parts = event_id.split("-")
    if len(parts) != 2:
        raise UsageError(
            f"event-id {event_id!r} is not in <start>-<end> form",
            hint=(
                "event-id values come from `motion history` JSONL — pipe "
                "the JSON line's `event_id` field directly. Got "
                f"{len(parts)} dash-separated parts."
            ),
        )
    try:
        start = int(parts[0])
        end = int(parts[1])
    except ValueError as exc:
        raise UsageError(
            f"event-id {event_id!r} parts are not integers",
            hint=(
                "event-id is <start_epoch>-<end_epoch> — both must parse "
                "as integers. Re-run `motion history` to get a valid id."
            ),
        ) from exc
    if end < start:
        raise UsageError(
            f"event-id {event_id!r} has end before start",
            hint="<start_epoch>-<end_epoch> must satisfy end >= start.",
        )
    return start, end


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
        sys.stdout.write("ts\ttype\tregion\thas_clip\tevent_id\n")
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
            str(record.get("event_id", "-")),
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


# ---------------------------------------------------------------------------
# Phase 4c — download-clip implementation (FR-63..65, §16.4.3)
# ---------------------------------------------------------------------------

# Backoff schedule (FR-64a) for transport errors during the segment-list
# fetch step. Three attempts max with 1s / 2s / 4s sleeps in between. We
# don't retry mid-byte-stream — partial files there get cleaned and the
# whole download restarts.
_DOWNLOAD_BACKOFF_SCHEDULE: tuple[float, float, float] = (1.0, 2.0, 4.0)
_DOWNLOAD_MAX_ATTEMPTS: int = 3

# Mechanism token surfaced in the FR-65 output envelope. Deliberate
# observability flag — operators key CI assertions off this value, so
# treat it as part of the public schema.
_DOWNLOAD_MECHANISM: str = "pytapo-experiments"


def _ffmpeg_on_path(binary: str = "ffmpeg") -> bool:
    """Return True if ``binary`` resolves on PATH (FR-64).

    pytapo's ``Downloader.download()`` shells ffmpeg to remux the on-disk
    segment files into a single MP4. Missing ffmpeg → ``ConfigError``
    (exit 6) for parity with the ``record`` verb's FR-13c gate.
    """
    return shutil.which(binary) is not None


async def _run_download_clip(
    *,
    target: str,
    event_id: str,
    output_path: str | None,
    experimental: bool,
    quiet_flag: bool,
    mode: OutputMode,
    timeout: float,
    config_path: object,
    credential_source: object,
) -> int:
    """Async coroutine driving the FR-63..65 clip download.

    Validation order (matters for exit-code stability — operators write
    cron rules against these):

    1. ``--experimental-clips`` opt-in (FR-63) → exit 64 if missing in
       non-tty mode; tty prompts to confirm.
    2. ``--output`` required → exit 64 if missing.
    3. ``event_id`` parses → exit 64 on malformed input.
    4. Group-target carve-out → exit 64 (FR-43c parity).
    5. ffmpeg-on-PATH gate → exit 6 (FR-64).
    6. Network connect + getEvents → exit 5 if SD-card-missing,
       exit 3 after retries on transport error.
    7. Event lookup → exit 4 if not found / has_clip:false (FR-63a/64a).
    8. pytapo download → exit 0 on success; cleanup on any exception.
    """
    # Step 1: --experimental-clips gate (FR-63).
    if not experimental:
        is_tty = sys.stdin.isatty() and sys.stderr.isatty()
        if is_tty and not quiet_flag:
            sys.stderr.write(
                "motion download-clip is EXPERIMENTAL and may break across "
                "firmware updates (SRD §16.4.3).\n"
                "Continue without --experimental-clips? [y/N] "
            )
            sys.stderr.flush()
            try:
                answer = sys.stdin.readline().strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer not in ("y", "yes"):
                raise UsageError(
                    "motion download-clip aborted: experimental opt-in "
                    "declined",
                    hint=(
                        "Re-run with --experimental-clips to bypass this "
                        "prompt. The clip-download path is experimental "
                        "and may break across firmware updates "
                        "(SRD §16.4.3)."
                    ),
                )
        else:
            raise UsageError(
                "motion download-clip requires --experimental-clips",
                hint=(
                    "The clip-download path is experimental and may "
                    "break across firmware updates. Pass "
                    "--experimental-clips to opt in (SRD §16.4.3, "
                    "FR-63)."
                ),
            )

    # Step 2: --output required.
    if output_path is None:
        raise UsageError(
            "motion download-clip requires --output PATH",
            hint=(
                "Pass --output /path/to/clip.mp4 — the verb writes the "
                "MP4 bytes to that path on success."
            ),
        )

    # Step 3: event_id parses (eager — exit 64 BEFORE network).
    start_epoch, end_epoch = parse_event_id(event_id)

    from tapo_cli.verbs._fanout import is_group_target

    cfg, _ = load_config_with_target(target, config_path)

    # Group fan-out is meaningless here — one event id per device. Reject
    # group targets explicitly with the same exit-64 carve-out as
    # ``stream`` / ``record`` / ``events`` (FR-43c parity).
    if is_group_target(target, cfg):
        raise UsageError(
            f"motion download-clip does not accept group target {target!r}",
            hint=(
                "Event ids are per-device. Loop in the shell or invoke "
                "once per alias with the matching event_id."
            ),
        )

    # FR-64: ffmpeg required on PATH. Probe BEFORE we open the network
    # connection so a misconfigured host fails fast.
    ffmpeg_bin = (cfg.ffmpeg.path or "ffmpeg") if hasattr(cfg, "ffmpeg") else "ffmpeg"
    if not _ffmpeg_on_path(ffmpeg_bin):
        raise ConfigError(
            f"ffmpeg not found on PATH: {ffmpeg_bin!r}",
            hint=(
                "Install ffmpeg (`brew install ffmpeg`, "
                "`apt install ffmpeg`) or set [ffmpeg] path = '...' in "
                "config.toml. Required for clip-segment remux "
                "(FR-64, §16.4.3)."
            ),
        )

    from tapo_cli import wrapper as wrap

    cfg, resolved_target = load_config_with_target(target, config_path)
    conn = await wrap.connect(
        cfg,
        resolved_target,
        credential_source=credential_source,  # type: ignore[arg-type]
        timeout=timeout,
    )

    # Resolve the event in the on-camera index. The index is the same
    # ``getEvents()`` payload ``motion history`` uses; we re-fetch with a
    # tight window so we don't pull the entire 24h history just to look
    # up one event.
    raw_events = await _fetch_events_with_retry(
        conn.tapo,
        start_time=float(start_epoch) - 60.0,
    )
    matched: dict[str, Any] | None = None
    for ev in raw_events:
        if not isinstance(ev, dict):
            continue
        ev_start = ev.get("start_time")
        ev_end = ev.get("end_time")
        if (
            isinstance(ev_start, (int, float))
            and isinstance(ev_end, (int, float))
            and int(ev_start) == start_epoch
            and int(ev_end) == end_epoch
        ):
            matched = ev
            break

    if matched is None:
        # FR-63a: unknown event-id → exit 4.
        raise NotFoundError(
            f"event_id {event_id!r} not found on {conn.target.alias}",
            target=conn.target.alias,
            hint=(
                "Re-run `motion history` to refresh ids; the on-camera "
                "SD-card index may have rolled over, or the device "
                "rebooted between history and download. If `motion "
                "history` returns no events at all, the camera likely "
                "has no SD card inserted."
            ),
        )

    # FR-64a: distinguish "event has no clip" from "no SD card at all".
    has_clip = bool(matched.get("video_id") or matched.get("has_clip"))
    if not has_clip:
        raise NotFoundError(
            f"event_id {event_id!r} has has_clip: false",
            target=conn.target.alias,
            hint=(
                "Motion was detected but the camera did not record a "
                "video clip — typical when the SD card is full, "
                "write-protected, or recording-on-motion is disabled. "
                "Check `motion status` and the SD-card mount state in "
                "the Tapo app."
            ),
        )

    # Hand off to pytapo's experiments-derived download path.
    download_start = time.monotonic()
    bytes_written = await _download_via_pytapo(
        tapo=conn.tapo,
        start_epoch=start_epoch,
        end_epoch=end_epoch,
        output_path=output_path,
    )
    elapsed_s = round(time.monotonic() - download_start, 3)

    record = {
        "target": conn.target.alias,
        "event_id": event_id,
        "output_path": output_path,
        "bytes": bytes_written,
        "duration_s": elapsed_s,
        "mechanism": _DOWNLOAD_MECHANISM,
    }
    emit(record, mode, formatter=_download_to_text)
    return EXIT_SUCCESS


def _download_to_text(record: object) -> str:
    if not isinstance(record, dict):
        return str(record)
    return "\t".join(
        [
            str(record.get("target", "-")),
            str(record.get("event_id", "-")),
            str(record.get("output_path", "-")),
            f"{record.get('bytes', 0)}B",
            f"{record.get('duration_s', 0)}s",
        ]
    )


async def _fetch_events_with_retry(tapo: Any, *, start_time: float) -> list[Any]:
    """Re-call ``getEvents`` with the FR-64a backoff schedule.

    Failure modes we explicitly distinguish:

    * pytapo raises an error containing ``-71112`` or the substring
      ``"playback"``: device reports no SD-card / playback index. We
      surface this as :class:`UnsupportedFeatureError` (exit 5) — the
      camera simply does not implement the recording API on this
      hardware. This is the dominant case on the live C200 with no SD
      card inserted.
    * pytapo raises a generic transport / network error: retry with the
      1s/2s/4s backoff (3 attempts max) before re-raising as
      :class:`NetworkError` (exit 3).
    """
    last_exc: Exception | None = None
    for attempt in range(_DOWNLOAD_MAX_ATTEMPTS):
        try:
            return await asyncio.to_thread(_call_get_events, tapo, start_time)
        except Exception as exc:
            msg = str(exc)
            if "-71112" in msg or "playback" in msg.lower():
                raise UnsupportedFeatureError(
                    "device has no SD card or recording API is unavailable",
                    hint=(
                        "The camera reports no playback/SD-card index. "
                        "Insert an SD card via the Tapo app and confirm "
                        "`motion history` returns at least one event "
                        "with `has_clip: true` before retrying."
                    ),
                    mechanism=_DOWNLOAD_MECHANISM,
                ) from exc
            last_exc = exc
            if attempt < _DOWNLOAD_MAX_ATTEMPTS - 1:
                logger.info(
                    "download-clip: getEvents attempt %d/%d failed (%s); "
                    "backing off %.1fs",
                    attempt + 1,
                    _DOWNLOAD_MAX_ATTEMPTS,
                    type(exc).__name__,
                    _DOWNLOAD_BACKOFF_SCHEDULE[attempt],
                )
                await asyncio.sleep(_DOWNLOAD_BACKOFF_SCHEDULE[attempt])
                continue
    raise NetworkError(
        f"download-clip: getEvents failed after {_DOWNLOAD_MAX_ATTEMPTS} "
        f"attempts: {last_exc!r}",
        hint=(
            "Transport errors during the segment-list fetch step. Check "
            "LAN reachability to the camera and re-run; the experiments-"
            "derived download path is fragile under network instability."
        ),
        mechanism=_DOWNLOAD_MECHANISM,
    ) from last_exc


async def _download_via_pytapo(
    *,
    tapo: Any,
    start_epoch: int,
    end_epoch: int,
    output_path: str,
) -> int:
    """Drive pytapo's ``Downloader`` to write the clip to ``output_path``.

    The pytapo ``experiments/DownloadRecordings.py`` reference script
    does roughly this:

    1. ``getTimeCorrection()`` → integer offset.
    2. ``Downloader(tapo, startTime, endTime, timeCorrection,
       outputDirectory, fileName=...)``.
    3. ``async for status in downloader.download(): ...``.

    The ``Downloader`` class lives in
    ``pytapo.media_stream.downloader`` (the ``experiments/`` folder
    contains call-sites, not packaged Python — that surface is NOT
    importable from the installed pytapo). We isolate the import here
    so test mocks can patch
    :func:`tapo_cli.verbs.motion_cmd._download_via_pytapo` without
    touching the experiments/ subtree or installing extras for tests
    that don't exercise the live download path.

    Cleanup contract:

    * Success → return the number of bytes written to ``output_path``.
    * Any exception from pytapo → delete the partial file (if it
      exists) before re-raising. This includes ``KeyboardInterrupt``
      (SIGINT) so the SIGINT-mid-download case leaves no stub file
      behind.
    """
    try:
        from pytapo.media_stream.downloader import Downloader  # type: ignore[import-untyped]
    except ImportError as exc:
        raise UnsupportedFeatureError(
            "pytapo does not expose the experiments/Downloader class",
            hint=(
                "The clip-download path needs "
                "`pytapo.media_stream.downloader.Downloader`. The "
                "pinned pytapo SHA in pyproject.toml ships it; if the "
                "import fails the wheel is corrupt — re-run "
                "`uv sync --reinstall`."
            ),
            mechanism=_DOWNLOAD_MECHANISM,
        ) from exc

    out_dir = os.path.dirname(os.path.abspath(output_path)) or "."
    out_name = os.path.basename(output_path) or f"{start_epoch}-{end_epoch}.mp4"

    # Time-correction lookup happens inside getEvents already (pytapo
    # caches it on the Tapo instance), but the experiments script calls
    # it explicitly so we mirror that to avoid surprising the upstream
    # contract.
    time_correction = await asyncio.to_thread(
        _safe_get_time_correction, tapo
    )

    downloader = Downloader(
        tapo,
        start_epoch,
        end_epoch,
        time_correction,
        out_dir,
        None,
        True,  # overwriteFiles — operators picked --output deliberately
        50,
        fileName=out_name,
    )

    try:
        # ``downloader.download()`` is an async generator that yields
        # status dicts; we consume to completion. Any transport error
        # bubbles up as the underlying exception type — we wrap it in
        # NetworkError before re-raising at the call site.
        async for _status in downloader.download():
            pass
    except (KeyboardInterrupt, asyncio.CancelledError):
        # SIGINT mid-download: clean partial file and re-raise. The
        # runner converts KeyboardInterrupt to exit 130 (FR runner
        # contract).
        _unlink_quiet(output_path)
        raise
    except Exception as exc:
        _unlink_quiet(output_path)
        raise NetworkError(
            f"download-clip: transport failure during ffmpeg-concat: "
            f"{type(exc).__name__}: {exc}",
            hint=(
                "The pytapo segment-stream connection dropped or the "
                "ffmpeg remux step failed. Check LAN stability and "
                "re-run; partial file removed."
            ),
            mechanism=_DOWNLOAD_MECHANISM,
        ) from exc

    if not os.path.isfile(output_path):
        # Defensive: pytapo can in theory write to a different filename
        # if ``out_name`` collides with an existing file in
        # ``out_dir``. We treat that as a network/transport failure
        # because the operator's contract (the file at --output) wasn't
        # honored.
        raise NetworkError(
            f"download-clip: pytapo did not write {output_path!r}",
            hint=(
                "The downloader returned without writing the requested "
                "output path — likely an upstream pytapo bug or a "
                "permission issue. Check the directory write bit and "
                "re-run."
            ),
            mechanism=_DOWNLOAD_MECHANISM,
        )

    return os.path.getsize(output_path)


def _safe_get_time_correction(tapo: Any) -> int:
    """Return ``getTimeCorrection()`` or 0 if pytapo can't supply one.

    Pytapo's ``Downloader`` accepts a small integer offset; falling back
    to 0 keeps the download path live on firmwares that don't expose
    the time-correction RPC.
    """
    try:
        rc = tapo.getTimeCorrection()
        return int(rc) if isinstance(rc, (int, float)) else 0
    except Exception:
        return 0


def _unlink_quiet(path: str) -> None:
    """Best-effort ``os.unlink`` — never raise from a cleanup path."""
    try:
        if os.path.isfile(path):
            os.unlink(path)
    except OSError:
        pass


__all__ = ["motion_cmd", "motion_download_clip", "parse_event_id"]
