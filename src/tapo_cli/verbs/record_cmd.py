"""``tapo-cli record <target> --output PATH`` (SRD §5.5, FR-13..13g, S3).

One-shot recording via an ``ffmpeg`` foreground child. The verb name
itself announces intent to write a file — there is NO ``--with-recording``
flag in v1.1 (S3 dropped it).

Footgun guard (FR-13a):

* In **non-tty mode** the verb requires ONE of ``--duration <seconds>``
  or ``--max-bytes <N>``. Open-ended recording from a script is exit 64.
* In **tty mode** without a cap, the verb prompts on stderr ("Record
  indefinitely until Ctrl-C? [y/N]") and aborts on ``N`` / empty.

Lifecycle (FR-13b, FR-13g):

* ``--duration`` → ``ffmpeg -t <seconds>`` (fixed-length).
* ``--max-bytes`` → ``ffmpeg -fs <N>`` (size-capped).
* SIGINT/SIGTERM on the CLI: forward the signal to ffmpeg, wait up to
  2 s for atom-finalization (the SRD says 5 s upper but sets 2 s as the
  typical-case target — we use 2 s grace, then SIGKILL).
* ``stop`` from natural completion → ``exit_reason: "complete"``.
* Group target (``@group``) → exit 64 (FR-43c, parity with stream).
* Camera-account credential is REQUIRED (FR-CRED-7); missing → exit 2.

Output (after the child terminates) is a single JSON record::

    {
      "target":           "<alias>",
      "output_path":      "<absolute path>",
      "duration_seconds": <float>,    # wall-clock measured by the CLI
      "bytes":            <int>,      # final size of the output file
      "exit_reason":      "complete"|"sigterm"|"sigint"|"max-bytes"|"max-duration"|"ffmpeg-error"
    }
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import click

from tapo_cli.config import load_config
from tapo_cli.credentials import resolve_camera_account
from tapo_cli.errors import (
    EXIT_SIGINT,
    EXIT_SIGTERM,
    EXIT_SUCCESS,
    ConfigError,
    DeviceError,
    UsageError,
)
from tapo_cli.media import build_rtsp_url
from tapo_cli.output import OutputMode, emit
from tapo_cli.runner import run_async as _run_async

logger = logging.getLogger("tapo_cli")


# ---------------------------------------------------------------------------
# Click verb
# ---------------------------------------------------------------------------


@click.command("record")
@click.argument("target", type=str)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False),
    required=True,
    help="Destination MP4 file. Parent directory must exist.",
)
@click.option(
    "--duration",
    "duration",
    type=int,
    default=None,
    help="Fixed-length recording in whole seconds (ffmpeg -t).",
)
@click.option(
    "--max-bytes",
    "max_bytes",
    type=int,
    default=None,
    help="Size-capped recording in bytes (ffmpeg -fs).",
)
@click.option(
    "--lens",
    type=click.Choice(["wide", "telephoto"]),
    default="wide",
    show_default=True,
    help="Lens for dual-lens cameras (FR-13e).",
)
@click.option(
    "--quality",
    type=click.Choice(["hd", "sd"]),
    default="hd",
    show_default=True,
    help="HD = main stream, SD = sub-stream (FR-13e).",
)
@click.option(
    "--protocol",
    "protocol_override",
    type=click.Choice(["stream1", "stream2", "stream6", "stream7"]),
    default=None,
    help="Override the lens/quality truth table with a specific stream path.",
)
@click.pass_context
def record_cmd(
    ctx: click.Context,
    *,
    target: str,
    output_path: str,
    duration: int | None,
    max_bytes: int | None,
    lens: str,
    quality: str,
    protocol_override: str | None,
) -> None:
    """Spawn ffmpeg to record one-shot from the camera RTSP feed (FR-13)."""
    state = ctx.obj
    mode: OutputMode = state["mode"]
    timeout = float(state.get("timeout") or 5.0)
    config_path = state.get("config_path")

    rc = _run_async(
        lambda: _run(
            target=target,
            output_path=output_path,
            duration=duration,
            max_bytes=max_bytes,
            lens=lens,
            quality=quality,
            protocol_override=protocol_override,
            mode=mode,
            timeout=timeout,
            config_path=config_path,
        ),
        mode=mode,
    )
    sys.exit(rc)


# ---------------------------------------------------------------------------
# Async core
# ---------------------------------------------------------------------------


async def _run(
    *,
    target: str,
    output_path: str,
    duration: int | None,
    max_bytes: int | None,
    lens: str,
    quality: str,
    protocol_override: str | None,
    mode: OutputMode,
    timeout: float,
    config_path: object,
) -> int:
    cfg_path = Path(str(config_path)).expanduser() if config_path else None
    cfg = load_config(cfg_path)

    resolved_target = target.lstrip("@") or target

    # FR-43c: record refuses group targets.
    if resolved_target in cfg.groups:
        raise UsageError(
            f"record does not accept group target {target!r}",
            hint=(
                "Recording from multiple cameras at once is a footgun. "
                "Loop in the shell or invoke once per device."
            ),
        )

    device = cfg.devices.get(resolved_target)
    if device is None:
        raise UsageError(
            f"unknown alias: {resolved_target!r}",
            hint="Run `tapo-cli list` to see configured aliases.",
        )
    if not device.ip:
        raise UsageError(
            f"alias {resolved_target!r} has no ip in config",
            hint=f"Add ip = '<address>' under [devices.{resolved_target}].",
        )

    # FR-13a: footgun guard.
    if duration is None and max_bytes is None:
        if sys.stdin.isatty() and sys.stderr.isatty():
            sys.stderr.write(
                "Record indefinitely until Ctrl-C? [y/N] "
            )
            sys.stderr.flush()
            try:
                answer = sys.stdin.readline().strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer not in ("y", "yes"):
                raise UsageError(
                    "record aborted: indefinite recording declined",
                    hint=(
                        "Pass --duration <seconds> or --max-bytes <N> "
                        "to set a cap."
                    ),
                )
        else:
            raise UsageError(
                "record in non-tty mode requires --duration or --max-bytes",
                hint=(
                    "Open-ended recording from a script is a footgun. Pick "
                    "a cap; tty users get an interactive prompt."
                ),
            )

    if duration is not None and duration <= 0:
        raise UsageError(
            f"--duration must be positive, got {duration}",
            hint="Pass an integer >= 1.",
        )
    if max_bytes is not None and max_bytes <= 0:
        raise UsageError(
            f"--max-bytes must be positive, got {max_bytes}",
            hint="Pass an integer >= 1.",
        )

    # FR-CRED-7: record is camera-account-only.
    cred = resolve_camera_account(cfg, alias=resolved_target)

    # Resolve stream path (FR-13e): same truth table as stream.
    stream_path = _resolve_stream_path(
        lens=lens, quality=quality, protocol_override=protocol_override
    )
    rtsp_url = build_rtsp_url(
        device.ip, cred.username, cred.password, path=stream_path
    )

    # ffmpeg on PATH? (FR-13c)
    ffmpeg_bin = cfg.ffmpeg.path or "ffmpeg"
    if not _ffmpeg_available(ffmpeg_bin):
        raise ConfigError(
            f"ffmpeg not found on PATH: {ffmpeg_bin!r}",
            hint=(
                "Install ffmpeg (`brew install ffmpeg` on macOS) or set "
                "[ffmpeg] path in config."
            ),
        )

    out_file = Path(output_path).expanduser()
    parent = out_file.parent
    if not parent.exists():
        raise UsageError(
            f"output directory does not exist: {parent}",
            hint="Create the directory or pass a different --output path.",
        )

    argv = _build_ffmpeg_argv(
        ffmpeg_bin=ffmpeg_bin,
        rtsp_url=rtsp_url,
        output_path=str(out_file),
        duration=duration,
        max_bytes=max_bytes,
    )

    del timeout  # reserved; ffmpeg drives its own RTSP connect timeout

    exit_reason, elapsed_seconds, returncode = _run_ffmpeg(argv)

    bytes_written = out_file.stat().st_size if out_file.exists() else 0

    record: dict[str, object] = {
        "target": resolved_target,
        "output_path": str(out_file),
        "duration_seconds": round(elapsed_seconds, 3),
        "bytes": bytes_written,
        "exit_reason": exit_reason,
    }

    emit(record, mode, formatter=lambda r: _to_text(r))

    if exit_reason == "sigint":
        return EXIT_SIGINT
    if exit_reason == "sigterm":
        return EXIT_SIGTERM
    if exit_reason == "ffmpeg-error":
        # ffmpeg reported a non-zero rc that wasn't due to our signal — the
        # most common case is auth / network failure mid-stream. Translate
        # to DeviceError so the runner emits a structured error envelope.
        raise DeviceError(
            f"ffmpeg exited with code {returncode}",
            target=resolved_target,
            mechanism="ffmpeg",
            hint="Check the camera is reachable and credentials are valid.",
        )
    return EXIT_SUCCESS


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------


_TRUTH_TABLE: dict[tuple[str, str], str] = {
    ("wide", "hd"): "stream1",
    ("wide", "sd"): "stream2",
    ("telephoto", "hd"): "stream6",
    ("telephoto", "sd"): "stream7",
}


def _resolve_stream_path(
    *, lens: str, quality: str, protocol_override: str | None
) -> str:
    if protocol_override:
        return protocol_override
    return _TRUTH_TABLE[(lens, quality)]


def _ffmpeg_available(ffmpeg_bin: str) -> bool:
    """Return True if ``ffmpeg_bin`` is on PATH or absolute and exists."""
    candidate = Path(ffmpeg_bin)
    if candidate.is_absolute():
        return candidate.exists() and os.access(candidate, os.X_OK)
    import shutil

    return shutil.which(ffmpeg_bin) is not None


def _build_ffmpeg_argv(
    *,
    ffmpeg_bin: str,
    rtsp_url: str,
    output_path: str,
    duration: int | None,
    max_bytes: int | None,
) -> list[str]:
    """Construct the ffmpeg argv list (FR-13d, FR-13d.1).

    We use ``-c:v copy`` (no video transcoding so CPU stays low on long
    recordings) plus ``-c:a aac`` (transcode audio to AAC because Tapo
    cameras emit PCM_ALAW which MP4 doesn't accept with ``-c copy``).
    ``-rtsp_transport tcp`` because UDP RTSP through home routers is
    brittle. ``-y`` overwrites the output silently.
    """
    argv = [
        ffmpeg_bin,
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
        "-i",
        rtsp_url,
        # Video stream is already H.264 — copy, don't re-encode (zero CPU).
        "-c:v",
        "copy",
        # Audio stream is PCM_ALAW from Tapo firmware; MP4 needs AAC.
        # ``-c:a aac`` transcodes; ``-b:a 128k`` is a sane default.
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-y",
    ]
    if duration is not None:
        argv += ["-t", str(duration)]
    if max_bytes is not None:
        argv += ["-fs", str(max_bytes)]
    argv.append(output_path)
    return argv


def _run_ffmpeg(argv: list[str]) -> tuple[str, float, int]:
    """Spawn ffmpeg, install signal forwarders, wait, return result.

    Returns ``(exit_reason, elapsed_seconds, returncode)``.

    Signal handling (FR-13b, FR-13g): the parent process catches SIGINT
    and SIGTERM, sends the same signal to ffmpeg, and waits up to 2 s
    for ffmpeg to flush and finalize the MP4 atom. After 2 s we SIGKILL.

    The signal handlers run on the main thread (Python's default), and
    the wait happens in this same thread via ``Popen.wait``. The runner
    has already swapped its own SIGINT/SIGTERM handlers around our verb,
    but those handlers raise ``KeyboardInterrupt`` / ``SystemExit`` —
    which we catch here so ffmpeg can finalize.
    """
    started = time.monotonic()

    # Spawn the child. ``start_new_session=True`` puts ffmpeg in its own
    # process group so a stray Ctrl-C in the controlling terminal can't
    # double-deliver SIGINT to ffmpeg before we forward it ourselves.
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    exit_reason = "complete"
    returncode = 0

    try:
        returncode = proc.wait()
    except KeyboardInterrupt:
        exit_reason = "sigint"
        _terminate_ffmpeg(proc, signal.SIGINT)
        returncode = proc.returncode if proc.returncode is not None else -1
    except SystemExit:
        exit_reason = "sigterm"
        _terminate_ffmpeg(proc, signal.SIGTERM)
        returncode = proc.returncode if proc.returncode is not None else -1
    finally:
        # Always close stderr so the pipe buffer doesn't leak.
        if proc.stderr is not None:
            import contextlib as _ctx
            with _ctx.suppress(Exception):
                proc.stderr.close()

    elapsed = time.monotonic() - started

    if exit_reason == "complete":
        if returncode == 0:
            # If max-bytes was passed and the file hit the cap, ffmpeg
            # exits 0 — distinguish via the argv we sent.
            if "-fs" in argv:
                exit_reason = "max-bytes"
            elif "-t" in argv:
                exit_reason = "max-duration"
            else:
                exit_reason = "complete"
        else:
            exit_reason = "ffmpeg-error"

    return exit_reason, elapsed, returncode


def _terminate_ffmpeg(proc: subprocess.Popen[bytes], sig: int) -> None:
    """Forward ``sig`` to ffmpeg, wait 2 s, SIGKILL if still alive (FR-13b/g)."""
    import contextlib as _ctx

    try:
        proc.send_signal(sig)
    except ProcessLookupError:
        return

    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        logger.warning(
            "ffmpeg did not exit within 2s of %s; sending SIGKILL",
            sig,
        )
        with _ctx.suppress(ProcessLookupError):
            proc.kill()
        with _ctx.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=2.0)


def _to_text(record: object) -> str:
    if not isinstance(record, dict):
        return str(record)
    return (
        f"{record.get('target', '-')}\t"
        f"output={record.get('output_path', '-')}\t"
        f"duration={record.get('duration_seconds')}s\t"
        f"bytes={record.get('bytes')}\t"
        f"reason={record.get('exit_reason', '-')}"
    )


__all__ = ["record_cmd"]
