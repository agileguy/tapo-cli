"""``tapo-cli audio <target> ...`` (FR-33..36, S4 capability gate).

Sub-verbs:

* ``audio volume <0-100>``      — set speaker volume (FR-33)
* ``audio mic mute|unmute|status``     — microphone capture (FR-34)
* ``audio speaker mute|unmute|status`` — speaker output (FR-35)
* ``audio tts <text>``          — TTS playback (FR-36) — gated per S4

Capability gating (S4):

* ``volume`` and ``speaker {mute,unmute,status}`` consult ``audio_speaker``.
* ``mic {mute,unmute,status}`` consults ``audio_mic``.
* ``tts <text>`` consults ``audio_tts``. The C200 (live test target) has
  ``audio_tts: false`` per the §3.3.1 matrix, so ``tts`` exits 5 with a
  hint listing the supported model prefixes.

pytapo signatures verified at the pinned SHA (de5ca37):

* ``setMicrophone(volume=None, mute=None, noise_cancelling=None)``
* ``setSpeakerVolume(volume)``  — accepts 0..100 integer
* No ``playTTS`` or equivalent is exposed at this SHA. The TTS verb gate
  fires BEFORE any pytapo call, so the absence is irrelevant — the gate
  rejects every model the matrix says lacks TTS.

Volume range (FR-33) is enforced before the capability check fires so
operators get a 64-style usage error for ``audio <target> volume 150``
even on a model that supports volume — the bad value is the user's
problem, not the model's.

Speaker mute is implemented as ``setSpeakerVolume(0)`` for mute and
``setSpeakerVolume(50)`` for unmute (a sensible default; a ``--restore``
flag for restoring prior volume is deferred to Phase 4 alongside the
volume-state cache). This best-effort matches the HA-Tapo-Control
behavior.

JSON output shape:

    {
      "target": "<alias>",
      "action": "volume|mic|speaker|tts",
      "subaction": "<mute|unmute|status>"  # for mic/speaker
      "volume": <int>                      # present on volume sub-verb
      "muted": <bool>                      # present on mic/speaker
    }
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

import click

from tapo_cli.errors import (
    EXIT_SUCCESS,
    UsageError,
)
from tapo_cli.output import OutputMode, emit
from tapo_cli.runner import run_async as _run_async
from tapo_cli.verbs._capability import require_feature, resolve_model_for_target
from tapo_cli.verbs._target import load_config_with_target

logger = logging.getLogger("tapo_cli")

# Default unmute volume — pytapo doesn't expose a "previous volume" knob,
# so we restore to a sensible mid-range value rather than guessing what
# the operator had set.
_DEFAULT_UNMUTE_VOLUME: int = 50


# ---------------------------------------------------------------------------
# Click verb tree
# ---------------------------------------------------------------------------


@click.group("audio")
@click.argument("target", type=str)
@click.pass_context
def audio_cmd(ctx: click.Context, target: str) -> None:
    """Volume / mic / speaker / TTS sub-verbs (FR-33..36)."""
    ctx.obj = dict(ctx.obj or {})
    ctx.obj["__audio_target__"] = target


@audio_cmd.command("volume")
@click.argument("level", type=int)
@click.pass_context
def audio_volume(ctx: click.Context, level: int) -> None:
    """Set speaker volume to LEVEL (0-100). FR-33."""
    # Range check happens inside ``_run`` so the error routes through the
    # runner's TapoCliError → exit-code mapping (exit 64 for usage errors).
    _dispatch(ctx, action="volume", subaction=None, level=level, text=None)


@audio_cmd.command("mic")
@click.argument("subaction", type=click.Choice(["mute", "unmute", "status"]))
@click.pass_context
def audio_mic(ctx: click.Context, subaction: str) -> None:
    """Microphone mute/unmute/status (FR-34)."""
    _dispatch(ctx, action="mic", subaction=subaction, level=0, text=None)


@audio_cmd.command("speaker")
@click.argument("subaction", type=click.Choice(["mute", "unmute", "status"]))
@click.pass_context
def audio_speaker(ctx: click.Context, subaction: str) -> None:
    """Speaker mute/unmute/status (FR-35)."""
    _dispatch(ctx, action="speaker", subaction=subaction, level=0, text=None)


@audio_cmd.command("tts")
@click.argument("text", type=str)
@click.pass_context
def audio_tts(ctx: click.Context, text: str) -> None:
    """Play TTS through the camera speaker (FR-36). Most models exit 5."""
    # Empty-text check happens inside ``_run`` so the error routes through
    # the runner's TapoCliError → exit-code mapping.
    _dispatch(ctx, action="tts", subaction=None, level=0, text=text)


# ---------------------------------------------------------------------------
# Coroutine dispatch
# ---------------------------------------------------------------------------


def _dispatch(
    ctx: click.Context,
    *,
    action: str,
    subaction: str | None,
    level: int,
    text: str | None,
) -> None:
    state = ctx.obj
    target = state["__audio_target__"]
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
            subaction=subaction,
            level=level,
            text=text,
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
    subaction: str | None,
    level: int,
    text: str | None,
    mode: OutputMode,
    timeout: float,
    config_path: object,
    credential_source: object,
) -> int:
    # Up-front usage-shape checks BEFORE we open a network connection. These
    # raise :class:`UsageError` (exit 64) without any pytapo round-trip.
    if action == "volume" and (level < 0 or level > 100):
        raise UsageError(
            f"audio volume must be in [0, 100], got {level}",
            target=target,
            hint="Use an integer between 0 (mute) and 100 (max).",
        )
    if action == "tts" and (text is None or not text.strip()):
        raise UsageError(
            "audio tts text must not be empty",
            target=target,
            hint="Provide non-empty text, e.g. `audio <target> tts \"hello\"`.",
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

    # Capability gate per S4.
    feature_for_action = {
        "volume": "audio_speaker",
        "mic": "audio_mic",
        "speaker": "audio_speaker",
        "tts": "audio_tts",
    }[action]
    require_feature(
        model=model,
        target=conn.target.alias,
        feature=feature_for_action,
        verb_name=f"audio {action}",
    )

    alias = conn.target.alias

    if action == "volume":
        await asyncio.to_thread(conn.tapo.setSpeakerVolume, level)
        record: dict[str, object] = {
            "target": alias,
            "action": "volume",
            "volume": level,
        }
    elif action == "mic":
        record = await _handle_mic(conn.tapo, alias, subaction)
    elif action == "speaker":
        record = await _handle_speaker(conn.tapo, alias, subaction)
    else:  # tts — gate above already exited 5 on unsupported models
        # If the gate passed (i.e. a future model row sets audio_tts: true)
        # we attempt the pytapo TTS verb. Pytapo at this SHA does not expose
        # one; surface a clear device-error envelope. When a future SHA
        # adds it, replace this branch.
        from tapo_cli.errors import DeviceError

        raise DeviceError(
            "TTS is not implemented in pytapo at the pinned SHA",
            target=alias,
            hint=(
                "Update pytapo and rebuild tapo-cli, or upstream a TTS "
                "playback verb to pytapo."
            ),
        )

    emit(record, mode, formatter=_to_text)
    return EXIT_SUCCESS


# ---------------------------------------------------------------------------
# Sub-action handlers
# ---------------------------------------------------------------------------


async def _handle_mic(
    tapo: Any, alias: str, subaction: str | None
) -> dict[str, object]:
    if subaction == "mute":
        await asyncio.to_thread(tapo.setMicrophone, None, True, None)
        return {
            "target": alias,
            "action": "mic",
            "subaction": "mute",
            "muted": True,
        }
    if subaction == "unmute":
        await asyncio.to_thread(tapo.setMicrophone, None, False, None)
        return {
            "target": alias,
            "action": "mic",
            "subaction": "unmute",
            "muted": False,
        }
    # status — best-effort read from getAudioConfig
    muted = await asyncio.to_thread(_read_mic_state, tapo)
    return {
        "target": alias,
        "action": "mic",
        "subaction": "status",
        "muted": muted,
    }


async def _handle_speaker(
    tapo: Any, alias: str, subaction: str | None
) -> dict[str, object]:
    if subaction == "mute":
        await asyncio.to_thread(tapo.setSpeakerVolume, 0)
        return {
            "target": alias,
            "action": "speaker",
            "subaction": "mute",
            "muted": True,
            "volume": 0,
        }
    if subaction == "unmute":
        await asyncio.to_thread(tapo.setSpeakerVolume, _DEFAULT_UNMUTE_VOLUME)
        return {
            "target": alias,
            "action": "speaker",
            "subaction": "unmute",
            "muted": False,
            "volume": _DEFAULT_UNMUTE_VOLUME,
        }
    # status — read getAudioConfig
    volume = await asyncio.to_thread(_read_speaker_volume, tapo)
    return {
        "target": alias,
        "action": "speaker",
        "subaction": "status",
        "muted": volume == 0 if volume is not None else False,
        "volume": volume if volume is not None else 0,
    }


def _read_mic_state(tapo: Any) -> bool:
    """Best-effort mic-state read from ``getAudioConfig``."""
    fn = getattr(tapo, "getAudioConfig", None)
    if not callable(fn):
        return False
    try:
        raw: object = fn()
    except Exception as exc:
        logger.debug("getAudioConfig failed during mic status: %s", exc)
        return False
    if not isinstance(raw, dict):
        return False
    mic = raw.get("microphone")
    if isinstance(mic, dict):
        muted = mic.get("mute_status") or mic.get("mute")
        if isinstance(muted, str):
            return muted.lower() in {"on", "true", "1", "muted"}
        if isinstance(muted, bool):
            return muted
    return False


def _read_speaker_volume(tapo: Any) -> int | None:
    """Best-effort speaker-volume read from ``getAudioConfig``."""
    fn = getattr(tapo, "getAudioConfig", None)
    if not callable(fn):
        return None
    try:
        raw: object = fn()
    except Exception as exc:
        logger.debug("getAudioConfig failed during speaker status: %s", exc)
        return None
    if not isinstance(raw, dict):
        return None
    speaker = raw.get("speaker")
    if isinstance(speaker, dict):
        vol = speaker.get("volume")
        if isinstance(vol, (int, float)):
            return int(vol)
        if isinstance(vol, str):
            try:
                return int(vol)
            except ValueError:
                return None
    return None


def _to_text(record: object) -> str:
    if not isinstance(record, dict):
        return str(record)
    parts = [f"{record.get('target', '-')}", f"action={record.get('action')}"]
    if "subaction" in record:
        parts.append(f"subaction={record['subaction']}")
    if "volume" in record:
        parts.append(f"volume={record['volume']}")
    if "muted" in record:
        parts.append(f"muted={record['muted']}")
    return "\t".join(parts)


__all__ = ["audio_cmd"]
