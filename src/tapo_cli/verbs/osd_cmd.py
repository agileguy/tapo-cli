"""``tapo-cli osd <target> ...`` (FR-37, S14).

Three sub-verbs covering the camera's on-screen display overlay:

* ``osd set --text "..." [--position bl|br|tl|tr] [--show-time]``
* ``osd clear``
* ``osd status``

Capability gating (S4):

* ``osd set --text`` consults ``osd_text``. C200 has ``osd_text: false``,
  so the verb exits 5 with a hint listing supported model prefixes.
* ``osd clear`` also consults ``osd_text`` — clearing a label is only
  meaningful on a model that can SET one in the first place.
* ``osd status`` and ``osd set --show-time`` consult ``osd_timestamp``.
  Every camera in the §3.3.1 matrix supports ``osd_timestamp: true``,
  so this gate effectively never fires in v1.

Length contract (FR-37a, S14):

* ``--text`` is measured in **Unicode codepoints** — a 32-codepoint cap.
* Inputs > 32 codepoints exit code 64 (``usage_error``) BEFORE any
  pytapo call. Single emoji, multi-byte CJK, and combining marks each
  count as one codepoint.

The capability gate fires BEFORE the codepoint check (because the gate
is the deeper invariant — there's no point complaining about length on
a model that doesn't support custom text at all). On C200 the gate
exits 5 even for a 1-character payload.

pytapo signature at the pinned SHA:

    setOsd(label, dateEnabled=True, labelEnabled=False, weekEnabled=False,
           logoEnabled=False, dateX=0, dateY=0, labelX=0, labelY=500,
           weekX=0, weekY=0, logoX=0, logoY=0)
    getOsd()

Position mapping (FR-37):

* Tapo OSD coordinates are integer (x, y) where x in [0..2880] and
  y in [0..1620] roughly map to a 16:9 frame's top-left origin.
* CLI positions are corner-only:
  - ``tl`` → labelX=0,    labelY=0
  - ``tr`` → labelX=2200, labelY=0
  - ``bl`` → labelX=0,    labelY=1500   (default per FR-37)
  - ``br`` → labelX=2200, labelY=1500
* Operators wanting fine-grained placement should drop down to the
  Tapo app for now — pixel-coordinate flags are deferred to Phase 4.

JSON output shapes:

* ``set``    : ``{"target", "action": "set", "text", "position", "show_time"}``
* ``clear``  : ``{"target", "action": "clear"}``
* ``status`` : ``{"target", "action": "status", "timestamp_on": <bool>,
                  "label": <str|null>, "label_on": <bool>}``
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
from tapo_cli.verbs._capability import require_feature, resolve_model_for_target
from tapo_cli.verbs._target import load_config_with_target

logger = logging.getLogger("tapo_cli")

_MAX_LABEL_CODEPOINTS: int = 32

# Corner → (labelX, labelY) coordinate mapping (FR-37).
_POSITION_COORDS: dict[str, tuple[int, int]] = {
    "tl": (0, 0),
    "tr": (2200, 0),
    "bl": (0, 1500),
    "br": (2200, 1500),
}


# ---------------------------------------------------------------------------
# Click verb tree
# ---------------------------------------------------------------------------


@click.group("osd")
@click.argument("target", type=str)
@click.pass_context
def osd_cmd(ctx: click.Context, target: str) -> None:
    """On-screen-display overlay sub-verbs (FR-37)."""
    ctx.obj = dict(ctx.obj or {})
    ctx.obj["__osd_target__"] = target


@osd_cmd.command("set")
@click.option("--text", "text", type=str, default=None, help="Label text (≤32 codepoints).")
@click.option(
    "--position",
    "position",
    type=click.Choice(["tl", "tr", "bl", "br"]),
    default="bl",
    show_default=True,
    help="Label corner position (default bottom-left).",
)
@click.option(
    "--show-time/--hide-time",
    "show_time",
    default=None,
    help="Toggle the timestamp overlay (independent of label).",
)
@click.pass_context
def osd_set(
    ctx: click.Context,
    text: str | None,
    position: str,
    show_time: bool | None,
) -> None:
    """Configure the OSD label and/or timestamp (FR-37)."""
    # Empty-args check happens inside ``_run`` so the error routes through
    # the runner's TapoCliError → exit-code mapping (exit 64).
    _dispatch(
        ctx,
        action="set",
        text=text,
        position=position,
        show_time=show_time,
    )


@osd_cmd.command("clear")
@click.pass_context
def osd_clear(ctx: click.Context) -> None:
    """Clear the OSD label (FR-37)."""
    _dispatch(ctx, action="clear", text=None, position="bl", show_time=None)


@osd_cmd.command("status")
@click.pass_context
def osd_status(ctx: click.Context) -> None:
    """Report current OSD state (FR-37). Always works when osd_timestamp=yes."""
    _dispatch(ctx, action="status", text=None, position="bl", show_time=None)


# ---------------------------------------------------------------------------
# Coroutine dispatch
# ---------------------------------------------------------------------------


def _dispatch(
    ctx: click.Context,
    *,
    action: str,
    text: str | None,
    position: str,
    show_time: bool | None,
) -> None:
    state = ctx.obj
    target = state["__osd_target__"]
    parent = ctx.parent
    parent_state = parent.obj if parent is not None else state
    mode: OutputMode = parent_state["mode"]
    timeout = float(parent_state.get("timeout") or 5.0)
    config_path = parent_state.get("config_path")
    credential_source = parent_state.get("credential_source")

    rc = _run_async(
        lambda: _run(
            target=target,
            action=action,
            text=text,
            position=position,
            show_time=show_time,
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
    text: str | None,
    position: str,
    show_time: bool | None,
    mode: OutputMode,
    timeout: float,
    config_path: object,
    credential_source: object,
) -> int:
    # Up-front usage-shape check BEFORE we open a network connection.
    if action == "set" and text is None and show_time is None:
        raise UsageError(
            "osd set requires at least one of --text or --show-time/--hide-time",
            target=target,
            hint=(
                "Pass --text \"...\" to set a label, or --show-time/"
                "--hide-time to toggle the timestamp."
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

    config_entry = cfg.devices.get(resolved_target)
    config_model = config_entry.model if config_entry is not None else None
    model = await resolve_model_for_target(conn.tapo, config_model=config_model)

    alias = conn.target.alias

    if action == "status":
        # Status only needs ``osd_timestamp`` — every model supports it,
        # but the gate is here for future-proofing.
        require_feature(
            model=model, target=alias, feature="osd_timestamp", verb_name="osd status"
        )
        return await _emit_status(conn.tapo, alias, mode)

    if action == "clear":
        # Clearing a label requires the model to support custom labels in
        # the first place. Otherwise there is nothing to clear.
        require_feature(
            model=model, target=alias, feature="osd_text", verb_name="osd clear"
        )
        await asyncio.to_thread(_apply_osd, conn.tapo, label="", label_enabled=False)
        clear_record: dict[str, object] = {"target": alias, "action": "clear"}
        emit(clear_record, mode, formatter=_to_text)
        return EXIT_SUCCESS

    # action == "set"
    if text is not None:
        # Capability gate FIRST — fail fast on unsupported models even for
        # a 1-character payload.
        require_feature(
            model=model, target=alias, feature="osd_text", verb_name="osd set"
        )
        # Codepoint length check (FR-37a, S14). len(str) on Python 3 IS the
        # codepoint count for unicode strings, NOT the byte count. A four-
        # byte CJK glyph counts as one codepoint; a single emoji counts as
        # one too (or sometimes two — surrogate pair / ZWJ — len() reflects
        # exactly what the SRD calls codepoints).
        if len(text) > _MAX_LABEL_CODEPOINTS:
            raise UsageError(
                f"osd --text exceeds {_MAX_LABEL_CODEPOINTS} codepoints "
                f"(got {len(text)})",
                target=alias,
                hint=f"Truncate the label to ≤{_MAX_LABEL_CODEPOINTS} codepoints.",
            )

    if show_time is not None:
        # Toggling the timestamp uses the lighter ``osd_timestamp`` gate
        # — no model in the matrix lacks it, but the capability check
        # is here for parity.
        require_feature(
            model=model, target=alias, feature="osd_timestamp", verb_name="osd set"
        )

    label_x, label_y = _POSITION_COORDS[position]
    await asyncio.to_thread(
        _apply_osd,
        conn.tapo,
        label=text or "",
        label_enabled=text is not None,
        label_x=label_x,
        label_y=label_y,
        date_enabled=show_time,
    )

    set_record: dict[str, object] = {
        "target": alias,
        "action": "set",
    }
    if text is not None:
        set_record["text"] = text
        set_record["position"] = position
    if show_time is not None:
        set_record["show_time"] = show_time

    emit(set_record, mode, formatter=_to_text)
    return EXIT_SUCCESS


async def _emit_status(tapo: Any, alias: str, mode: OutputMode) -> int:
    raw: object = await asyncio.to_thread(tapo.getOsd)
    record: dict[str, object] = {
        "target": alias,
        "action": "status",
        "timestamp_on": False,
        "label": None,
        "label_on": False,
    }
    if isinstance(raw, dict):
        record["timestamp_on"] = _coerce_bool(raw.get("dateEnabled"))
        record["label_on"] = _coerce_bool(raw.get("labelEnabled"))
        label = raw.get("label")
        if isinstance(label, str):
            record["label"] = label

    emit(record, mode, formatter=_to_text)
    return EXIT_SUCCESS


def _apply_osd(
    tapo: Any,
    *,
    label: str,
    label_enabled: bool,
    label_x: int = 0,
    label_y: int = 1500,
    date_enabled: bool | None = None,
) -> None:
    """Drive ``setOsd`` with our flag set.

    pytapo's signature requires ``label`` as the first positional argument
    and accepts boolean toggles for date/label/week/logo plus integer
    coordinates. ``date_enabled=None`` means "leave the device's existing
    timestamp setting alone" — we read it via ``getOsd`` first and pass
    the round-tripped value back.
    """
    effective_date_enabled = date_enabled
    if effective_date_enabled is None:
        try:
            current: object = tapo.getOsd()
            if isinstance(current, dict):
                effective_date_enabled = _coerce_bool(current.get("dateEnabled"))
            else:
                effective_date_enabled = True
        except Exception:
            effective_date_enabled = True

    tapo.setOsd(
        label,
        effective_date_enabled,
        label_enabled,
        False,  # weekEnabled
        False,  # logoEnabled
        0,      # dateX
        0,      # dateY
        label_x,
        label_y,
        0,      # weekX
        0,      # weekY
        0,      # logoX
        0,      # logoY
    )


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"on", "true", "1", "enabled", "yes"}
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _to_text(record: object) -> str:
    if not isinstance(record, dict):
        return str(record)
    parts = [f"{record.get('target', '-')}", f"action={record.get('action')}"]
    for key in ("text", "position", "show_time", "timestamp_on", "label", "label_on"):
        if key in record:
            parts.append(f"{key}={record[key]}")
    return "\t".join(parts)


__all__ = ["osd_cmd"]
