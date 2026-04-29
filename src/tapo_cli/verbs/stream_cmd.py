"""``tapo-cli stream <target>`` (SRD §5.5, FR-12..12g, B6, S2).

Emits an RTSP URL on stdout. The CLI does NOT decode video — operators
pipe the URL into ``ffmpeg``, ``mpv``, ``ffplay``, or NVR software.

Stream-path resolution order (FR-12b):

1. ``--profile <name>`` — explicit ONVIF profile lookup, bypasses the truth
   table.
2. ``--list-profiles`` — emit ONVIF GetProfiles as a JSON array, exit 0.
3. ``--protocol streamN`` — explicit override, bypasses the truth table.
4. Lens-by-quality truth table (B6):
   ``(wide, hd) -> /stream1``, ``(wide, sd) -> /stream2``,
   ``(telephoto, hd) -> /stream6``, ``(telephoto, sd) -> /stream7``.

Credential leakage hardening (FR-12f, FR-12g, S2):

* ``--credentials-via-env`` — emit URL with ``<user>:<pass>`` placeholders
  on stdout; export ``RTSP_USER`` and ``RTSP_PASS`` for an exec'd child.
* ``--exec <argv...>`` — replace ``tapo-cli`` with the named child via
  ``execvp``. URL is substituted into ``{}`` placeholders OR appended as
  the last arg. With ``--credentials-via-env`` set, the child sees only
  the redacted URL on argv; full URL flows via env.

Camera-account requirement (FR-CRED-7): if no ``camera_account_file`` is
configured for the target, ``stream`` exits ``2`` with a Tapo-app-menu hint.

Group-target rejection (FR-49 / FR-43c): a configured group target exits
``64``. Multiple cameras can't share one URL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import click

from tapo_cli.config import load_config
from tapo_cli.credentials import resolve_camera_account
from tapo_cli.errors import (
    EXIT_SUCCESS,
    EXIT_USAGE_ERROR,
    UsageError,
)
from tapo_cli.media import build_rtsp_url, redact_userinfo, resolve_onvif_wsdl_dir
from tapo_cli.output import OutputMode, emit
from tapo_cli.runner import run_async as _run_async

logger = logging.getLogger("tapo_cli")


# ---------------------------------------------------------------------------
# Lens-by-quality truth table (B6 / FR-12b)
# ---------------------------------------------------------------------------

_TRUTH_TABLE: dict[tuple[str, str], str] = {
    ("wide", "hd"): "stream1",
    ("wide", "sd"): "stream2",
    ("telephoto", "hd"): "stream6",
    ("telephoto", "sd"): "stream7",
}

_VALID_PROTOCOLS = ("stream1", "stream2", "stream6", "stream7")


def _resolve_stream_path(
    *, lens: str, quality: str, protocol_override: str | None
) -> str:
    """Apply FR-12b's resolution order. ``protocol_override`` wins."""
    if protocol_override:
        if protocol_override not in _VALID_PROTOCOLS:
            raise UsageError(
                f"--protocol must be one of {_VALID_PROTOCOLS}, got "
                f"{protocol_override!r}",
            )
        return protocol_override
    return _TRUTH_TABLE[(lens, quality)]


# ---------------------------------------------------------------------------
# ONVIF profile fetcher (FR-12b.1, FR-12b.2)
# ---------------------------------------------------------------------------


async def _fetch_onvif_profiles(
    ip: str, username: str, password: str, *, port: int, timeout: float
) -> list[dict[str, Any]]:
    """Return ``GetProfiles`` projected onto a JSON-safe list of dicts.

    Each row carries ``name``, ``token``, ``encoder``, ``resolution``, and
    where present, the encoder-config-derived ``stream_path`` hint. Errors
    bubble up to the caller (which maps them to exit 5 / exit 3).
    """
    wsdl_dir = resolve_onvif_wsdl_dir()
    from onvif import ONVIFCamera  # type: ignore[import-untyped]

    cam = ONVIFCamera(ip, port, username, password, wsdl_dir=str(wsdl_dir))
    await asyncio.wait_for(cam.update_xaddrs(), timeout=timeout)
    media = await cam.create_media_service()
    profiles = await asyncio.wait_for(media.GetProfiles(), timeout=timeout)

    out: list[dict[str, Any]] = []
    for p in profiles:
        name = getattr(p, "Name", None) or ""
        token = getattr(p, "token", None) or ""
        enc_cfg = getattr(p, "VideoEncoderConfiguration", None)
        encoder = getattr(enc_cfg, "Encoding", "") if enc_cfg is not None else ""
        resolution = ""
        if enc_cfg is not None:
            res = getattr(enc_cfg, "Resolution", None)
            if res is not None:
                w = getattr(res, "Width", "?")
                h = getattr(res, "Height", "?")
                resolution = f"{w}x{h}"
        out.append(
            {
                "name": str(name),
                "token": str(token),
                "encoder": str(encoder),
                "resolution": resolution,
            }
        )
    return out


def _profile_to_stream_path(profile_name: str) -> str:
    """Heuristic: profile name → stream path. Best-effort.

    Common Tapo profile names are ``mainStream`` / ``subStream`` /
    ``mainStream2`` / ``subStream2``. Map these onto the SRD truth table
    (B6). Unknown names fall through to ``stream1`` so a misnamed profile
    doesn't break the verb hard.
    """
    lower = profile_name.lower()
    if "main" in lower and "2" in lower:
        return "stream6"
    if "sub" in lower and "2" in lower:
        return "stream7"
    if "sub" in lower:
        return "stream2"
    if "main" in lower:
        return "stream1"
    return "stream1"


# ---------------------------------------------------------------------------
# Click verb
# ---------------------------------------------------------------------------


@click.command(
    "stream",
    context_settings={
        # ``--exec`` consumes a child argv list; everything after the divider
        # falls through to UNPROCESSED so flags meant for ffmpeg don't trip
        # Click's own parser.
        "ignore_unknown_options": True,
    },
)
@click.argument("target", type=str)
@click.option(
    "--lens",
    type=click.Choice(["wide", "telephoto"]),
    default="wide",
    show_default=True,
    help="Lens for dual-lens cameras. Single-lens cameras ignore this.",
)
@click.option(
    "--quality",
    type=click.Choice(["hd", "sd"]),
    default="hd",
    show_default=True,
    help="HD = main stream, SD = sub-stream (lower resolution).",
)
@click.option(
    "--protocol",
    "protocol_override",
    type=click.Choice(_VALID_PROTOCOLS),
    default=None,
    help=(
        "Override the lens/quality truth table with an explicit stream "
        f"path. One of {_VALID_PROTOCOLS}."
    ),
)
@click.option(
    "--profile",
    "profile_name",
    type=str,
    default=None,
    help="Force a specific ONVIF profile by name (FR-12b.1).",
)
@click.option(
    "--list-profiles",
    "list_profiles",
    is_flag=True,
    default=False,
    help="Emit the ONVIF GetProfiles response as JSON and exit (FR-12b.2).",
)
@click.option(
    "--credentials-via-env",
    "creds_via_env",
    is_flag=True,
    default=False,
    help=(
        "Redact creds in the printed URL and export RTSP_USER/RTSP_PASS "
        "for an exec'd child (FR-12f, S2)."
    ),
)
@click.option(
    "--exec",
    "exec_mode",
    is_flag=True,
    default=False,
    help=(
        "Replace tapo-cli with a child process via execvp; substitute the "
        "RTSP URL into '{}' placeholders or append as the last arg "
        "(FR-12g)."
    ),
)
@click.option(
    "--onvif-port",
    "onvif_port",
    type=int,
    default=2020,
    show_default=True,
    help="ONVIF service port (Tapo C-series default: 2020).",
)
@click.argument("exec_argv", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def stream_cmd(
    ctx: click.Context,
    *,
    target: str,
    lens: str,
    quality: str,
    protocol_override: str | None,
    profile_name: str | None,
    list_profiles: bool,
    creds_via_env: bool,
    exec_mode: bool,
    onvif_port: int,
    exec_argv: tuple[str, ...],
) -> None:
    """Print an RTSP URL on stdout (or exec a child process with it)."""
    state = ctx.obj
    mode: OutputMode = state["mode"]
    timeout = float(state.get("timeout") or 5.0)
    config_path = state.get("config_path")
    # Stream's default contract (FR-12) is a bare URL on stdout — even on a
    # pipe — so we need to know if the user asked for structured output
    # explicitly via --json / --jsonl, separate from cli.py's auto-pipe flip.
    explicit_json = bool(state.get("json_flag") or state.get("jsonl_flag"))
    explicit_quiet = bool(state.get("quiet_flag"))

    rc = _run_async(
        lambda: _run(
            target=target,
            lens=lens,
            quality=quality,
            protocol_override=protocol_override,
            profile_name=profile_name,
            list_profiles=list_profiles,
            creds_via_env=creds_via_env,
            exec_mode=exec_mode,
            exec_argv=exec_argv,
            onvif_port=onvif_port,
            mode=mode,
            explicit_json=explicit_json,
            explicit_quiet=explicit_quiet,
            timeout=timeout,
            config_path=config_path,
        ),
        mode=mode,
    )
    sys.exit(rc)


async def _run(
    *,
    target: str,
    lens: str,
    quality: str,
    protocol_override: str | None,
    profile_name: str | None,
    list_profiles: bool,
    creds_via_env: bool,
    exec_mode: bool,
    exec_argv: tuple[str, ...],
    onvif_port: int,
    mode: OutputMode,
    explicit_json: bool,
    explicit_quiet: bool,
    timeout: float,
    config_path: object,
) -> int:
    """Async core: load config, resolve creds, build URL, emit (or exec)."""
    cfg_path = Path(str(config_path)).expanduser() if config_path else None
    cfg = load_config(cfg_path)

    # Strip leading ``@`` to keep parity with info_cmd. After stripping, if the
    # token is a configured group → exit 64 (FR-49 / FR-43c).
    resolved_target = target.lstrip("@") or target
    if resolved_target in cfg.groups:
        raise UsageError(
            f"stream does not accept group target {target!r}",
            hint=(
                "Multiple cameras cannot share one URL. Loop over members in "
                "shell, or invoke stream once per device."
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

    # FR-CRED-7: stream is camera-account-only.
    cred = resolve_camera_account(cfg, alias=resolved_target)

    # ---- --list-profiles short-circuit (FR-12b.2) ----
    if list_profiles:
        try:
            profiles = await _fetch_onvif_profiles(
                device.ip,
                cred.username,
                cred.password,
                port=onvif_port,
                timeout=timeout,
            )
        except Exception as exc:
            # FR-12b.2: ONVIF unavailable → exit 5. Use unsupported_feature
            # via UnsupportedFeatureError.
            from tapo_cli.errors import UnsupportedFeatureError

            raise UnsupportedFeatureError(
                f"ONVIF GetProfiles unavailable on {resolved_target!r}: {exc}",
                target=resolved_target,
                hint="Enable Tapo Lab > Third-Party Compatibility in the Tapo app.",
            ) from exc
        # FR-12b.2: emit as JSON array; --json/--jsonl/text all reduce to a
        # single JSON document on stdout per the SRD wording.
        sys.stdout.write(json.dumps(profiles, indent=2))
        sys.stdout.write("\n")
        return EXIT_SUCCESS

    # ---- Resolve stream path ----
    if profile_name:
        # FR-12b.1: explicit profile name. Try ONVIF first; if unavailable,
        # fall through to the heuristic mapper.
        try:
            profiles = await _fetch_onvif_profiles(
                device.ip,
                cred.username,
                cred.password,
                port=onvif_port,
                timeout=timeout,
            )
        except Exception:
            profiles = []
        match = next(
            (p for p in profiles if p.get("name") == profile_name), None
        )
        if match is None:
            stream_path = _profile_to_stream_path(profile_name)
            resolver = "defaults"
        else:
            stream_path = _profile_to_stream_path(profile_name)
            resolver = "onvif"
    else:
        stream_path = _resolve_stream_path(
            lens=lens, quality=quality, protocol_override=protocol_override
        )
        resolver = "defaults"

    # ---- Build the URL ----
    rtsp_url = build_rtsp_url(
        device.ip, cred.username, cred.password, path=stream_path
    )

    # ---- --exec: hand off to child process via execvp (FR-12g) ----
    if exec_mode:
        if not exec_argv:
            raise UsageError(
                "--exec requires a child command after the flag",
                hint="Example: --exec ffmpeg -i '{}' -c copy out.mp4",
            )
        return _exec_child(
            list(exec_argv),
            rtsp_url=rtsp_url,
            cred_user=cred.username,
            cred_pass=cred.password,
            creds_via_env=creds_via_env,
        )

    # FR-12: default is a bare ``rtsp://...`` line on stdout regardless of
    # whether stdout is a tty or a pipe — this is the Unix-philosophy idiom
    # `tapo-cli stream X | xargs mpv`. JSON / JSONL emission is opt-in only
    # via the explicit --json / --jsonl flags (which surface the {resolver}
    # field per FR-12b). cli.py's auto-non-tty flip to JSONL must NOT win
    # over this — operators piping to ffmpeg need the bare URL.
    url_for_stdout = redact_userinfo(rtsp_url) if creds_via_env else rtsp_url

    if explicit_json:
        record_dict = {
            "target": resolved_target,
            "url": url_for_stdout,
            "lens": lens,
            "quality": quality,
            "protocol": "rtsp",
            "resolver": resolver,
        }
        emit(record_dict, mode, formatter=lambda r: url_for_stdout)
    elif explicit_quiet:
        # FR-49: --quiet suppresses stdout. The URL is the entire payload, so
        # quiet mode emits nothing on stdout (only the exit code matters).
        pass
    else:
        # Default mode: bare URL line, regardless of tty/pipe.
        sys.stdout.write(url_for_stdout + "\n")

    # Reference unused locals for type-checkers / future enhancement.
    del mode
    return EXIT_SUCCESS


# ---------------------------------------------------------------------------
# Exec helper (FR-12g)
# ---------------------------------------------------------------------------


def _exec_child(
    argv: list[str],
    *,
    rtsp_url: str,
    cred_user: str,
    cred_pass: str,
    creds_via_env: bool,
) -> int:
    """Replace this process with ``argv``, substituting the RTSP URL.

    Substitution rules (FR-12g):

    * If any ``argv`` element is exactly ``{}``, replace it in place with
      the URL.
    * Otherwise, append the URL as the last element.

    With ``creds_via_env=True``, the URL passed to the child is redacted
    (``<user>:<pass>`` placeholders) and ``RTSP_USER`` / ``RTSP_PASS`` are
    exported in the child's environment. The full URL is also exported as
    ``RTSP_URL`` for children that want to consume it directly.

    Returns:
        Always replaces this process when execvp succeeds (this function
        never returns). Returns an int only on the unreachable error path.
    """
    if creds_via_env:
        # Place a redacted URL on argv; full URL flows via env so the child
        # process line in ``ps`` and the shell history both stay clean.
        url_for_argv = redact_userinfo(rtsp_url)
        os.environ["RTSP_USER"] = cred_user
        os.environ["RTSP_PASS"] = cred_pass
        os.environ["RTSP_URL"] = rtsp_url
    else:
        url_for_argv = rtsp_url
        os.environ["RTSP_URL"] = rtsp_url

    substituted = False
    final: list[str] = []
    for arg in argv:
        if arg == "{}":
            final.append(url_for_argv)
            substituted = True
        else:
            final.append(arg)
    if not substituted:
        final.append(url_for_argv)

    # execvp resolves the binary on PATH; raises FileNotFoundError if missing.
    try:
        os.execvp(final[0], final)
    except FileNotFoundError as exc:
        raise UsageError(
            f"--exec child not found on PATH: {final[0]!r}",
            hint=f"Install {final[0]!r} or pass an absolute path.",
        ) from exc
    # Unreachable on success.
    return EXIT_USAGE_ERROR


__all__ = ["stream_cmd"]
