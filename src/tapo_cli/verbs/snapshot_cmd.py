"""``tapo-cli snapshot <target>`` (SRD §5.4, FR-11..11d, B5).

Three-mechanism fallback chain: pytapo native snapshot → ONVIF
``GetSnapshotUri`` → ``ffmpeg`` single-frame from RTSP. Each tier gets a
slice of the total ``--timeout`` budget (default 40% / 30% / 30%, override
via ``--snapshot-budget``). Tier-advance condition (FR-11a.1):

* per-tier budget elapsed without a complete response, OR
* non-200 HTTP / non-JPEG payload (sniffed via ``FF D8 FF`` magic bytes), OR
* any unhandled exception OTHER than auth-rejection.

Auth-rejection at ANY tier (HTTP 401, pytapo ``_AUTH_FAILED``, RTSP 401)
short-circuits the chain and exits ``2`` (FR-11a.2). If tier 3 is reached
and ``ffmpeg`` is missing on PATH, the verb exits ``6`` (config error,
FR-11a.4) — NOT ``1`` (device error). The mechanism that succeeded is
reported in ``--json`` output as ``mechanism`` (FR-11b).

``--output PATH`` writes the JPEG to a file (mode 0644). ``--output -``
writes binary JPEG bytes to stdout — incompatible with ``--json`` /
``--jsonl`` (exit 64, FR-11d). ``--quiet`` is permitted with ``--output -``
(S15 carve-out: the JPEG bytes ARE the stdout payload).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from tapo_cli.config import Config, load_config
from tapo_cli.errors import (
    EXIT_SUCCESS,
    EXIT_USAGE_ERROR,
    AuthError,
    ConfigError,
    DeviceError,
    UsageError,
)
from tapo_cli.media import (
    build_rtsp_url,
    mask_url_credentials,
    resolve_onvif_wsdl_dir,
)
from tapo_cli.output import OutputMode, emit, utc_now_rfc3339
from tapo_cli.runner import run_async as _run_async

logger = logging.getLogger("tapo_cli")


# ---------------------------------------------------------------------------
# Tier-result + auth-rejection sentinel
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _TierResult:
    """One mechanism's outcome.

    ``status`` is one of:
        ``"pass"`` — JPEG bytes captured.
        ``"fail"`` — tier failed; advance to next.
        ``"unavailable"`` — tier could not even attempt (e.g. no API);
            advance, but skip detail noise.
    Auth-rejection raises :class:`_AuthRejectedError` and never appears as a
    ``_TierResult`` because it short-circuits the whole chain.
    """

    mechanism: str  # "pytapo" | "onvif" | "ffmpeg"
    status: str  # "pass" | "fail" | "unavailable"
    elapsed_ms: float
    payload: bytes | None = None
    detail: str | None = None


class _AuthRejectedError(Exception):
    """Internal sentinel: HTTP 401 / pytapo _AUTH_FAILED / RTSP 401.

    Caught by the chain dispatcher, which emits a structured auth_failed
    error and exits 2 (FR-11a.2).
    """

    def __init__(self, mechanism: str, detail: str) -> None:
        super().__init__(f"{mechanism}: {detail}")
        self.mechanism = mechanism
        self.detail = detail


# ---------------------------------------------------------------------------
# Budget parsing (FR-11a.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Budget:
    pytapo: float
    onvif: float
    ffmpeg: float

    @property
    def total(self) -> float:
        return self.pytapo + self.onvif + self.ffmpeg


def _default_budget(total_timeout: float) -> _Budget:
    """40% pytapo / 30% ONVIF / 30% ffmpeg per FR-11a.3."""
    return _Budget(
        pytapo=round(total_timeout * 0.40, 3),
        onvif=round(total_timeout * 0.30, 3),
        ffmpeg=round(total_timeout * 0.30, 3),
    )


def _parse_budget_override(spec: str, total_timeout: float) -> _Budget:
    """Parse ``pytapo=N,onvif=N,ffmpeg=N`` (any subset, any order).

    Missing keys fall back to the 40/30/30 default split. Sum > total_timeout
    raises :class:`UsageError` (exit 64, FR-11a.3).
    """
    defaults = _default_budget(total_timeout)
    values: dict[str, float] = {
        "pytapo": defaults.pytapo,
        "onvif": defaults.onvif,
        "ffmpeg": defaults.ffmpeg,
    }
    if spec:
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            if "=" not in part:
                raise UsageError(
                    f"--snapshot-budget entry must be key=value, got {part!r}",
                    hint="Example: --snapshot-budget pytapo=2,onvif=2,ffmpeg=2",
                )
            key, raw = part.split("=", 1)
            key = key.strip()
            if key not in values:
                raise UsageError(
                    f"--snapshot-budget unknown key: {key!r}",
                    hint="Allowed keys: pytapo, onvif, ffmpeg",
                )
            try:
                values[key] = float(raw)
            except ValueError as exc:
                raise UsageError(
                    f"--snapshot-budget value for {key!r} is not numeric: {raw!r}"
                ) from exc

    budget = _Budget(
        pytapo=values["pytapo"], onvif=values["onvif"], ffmpeg=values["ffmpeg"]
    )
    if budget.total > total_timeout + 1e-9:
        raise UsageError(
            f"--snapshot-budget sum {budget.total:.3f}s exceeds --timeout "
            f"{total_timeout:.3f}s",
            hint="Reduce per-tier budgets or raise --timeout.",
        )
    return budget


# ---------------------------------------------------------------------------
# JPEG validation
# ---------------------------------------------------------------------------


_JPEG_MAGIC = b"\xff\xd8\xff"


def _is_jpeg(payload: bytes) -> bool:
    """FR-11a.1(b): magic-byte sniff the first 3 bytes for ``FF D8 FF``."""
    return len(payload) >= 3 and payload[:3] == _JPEG_MAGIC


def _jpeg_dimensions(payload: bytes) -> tuple[int | None, int | None]:
    """Parse JPEG SOF0/SOF2 marker for width/height; ``(None, None)`` on parse fail.

    Pure-Python — no Pillow dependency. Walks markers from offset 2; on the
    first SOFn (Start Of Frame) marker, reads height + width from the segment.
    """
    try:
        i = 2  # skip SOI
        n = len(payload)
        while i + 4 <= n:
            if payload[i] != 0xFF:
                return None, None
            marker = payload[i + 1]
            # Standalone markers (no length): RSTn (0xD0..0xD7), SOI/EOI/TEM.
            if 0xD0 <= marker <= 0xD9 or marker == 0x01:
                i += 2
                continue
            seg_len = (payload[i + 2] << 8) | payload[i + 3]
            # SOF markers: C0..CF except C4 (DHT), C8 (JPG), CC (DAC).
            if marker in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }:
                if i + 9 > n:
                    return None, None
                height = (payload[i + 5] << 8) | payload[i + 6]
                width = (payload[i + 7] << 8) | payload[i + 8]
                return width, height
            i += 2 + seg_len
    except (IndexError, ValueError):
        return None, None
    return None, None


# ---------------------------------------------------------------------------
# Tier 1: pytapo native snapshot
# ---------------------------------------------------------------------------


async def _tier1_pytapo(
    target_alias: str,
    ip: str,
    username: str,
    password: str,
    *,
    budget: float,
) -> _TierResult:
    """Try pytapo's native snapshot path with a wall-clock budget.

    pytapo at the pinned SHA does not expose a public single-frame API
    (Phase 0 confirmed). We attempt ``getMediaSession()`` as a liveness
    probe; if pytapo grows a real snapshot method later, this tier is
    where it goes. Auth failure raises :class:`_AuthRejectedError`.
    """
    start = time.perf_counter()

    def _run() -> bytes | None:
        try:
            from pytapo import Tapo  # type: ignore[import-untyped]
        except ImportError:
            return None
        tapo = Tapo(ip, username, password)
        # Best-effort: prefer getJpegSnapshot if a future pytapo release adds
        # one; fall through to None otherwise.
        for attr in ("getJpegSnapshot", "getSnapshot", "snapshot"):
            fn = getattr(tapo, attr, None)
            if callable(fn):
                result = fn()
                if isinstance(result, bytes):
                    return result
        return None

    try:
        payload = await asyncio.wait_for(asyncio.to_thread(_run), timeout=budget)
    except TimeoutError:
        return _TierResult(
            mechanism="pytapo",
            status="fail",
            elapsed_ms=(time.perf_counter() - start) * 1000.0,
            detail=f"timeout after {budget:.2f}s",
        )
    except Exception as exc:
        msg = str(exc).lower()
        if "_auth_failed" in msg or "401" in msg or "unauthorized" in msg:
            raise _AuthRejectedError("pytapo", str(exc)) from exc
        return _TierResult(
            mechanism="pytapo",
            status="fail",
            elapsed_ms=(time.perf_counter() - start) * 1000.0,
            detail=f"{type(exc).__name__}: {exc}",
        )

    elapsed = (time.perf_counter() - start) * 1000.0
    if payload is None:
        return _TierResult(
            mechanism="pytapo",
            status="unavailable",
            elapsed_ms=elapsed,
            detail="pytapo at pinned SHA has no native single-frame snapshot API",
        )
    if not _is_jpeg(payload):
        return _TierResult(
            mechanism="pytapo",
            status="fail",
            elapsed_ms=elapsed,
            detail=f"non-JPEG payload (first 3 bytes: {payload[:3]!r})",
        )
    return _TierResult(
        mechanism="pytapo",
        status="pass",
        elapsed_ms=elapsed,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Tier 2: ONVIF GetSnapshotUri
# ---------------------------------------------------------------------------


async def _tier2_onvif(
    ip: str,
    username: str,
    password: str,
    *,
    onvif_port: int,
    budget: float,
) -> _TierResult:
    """Fetch a JPEG via ONVIF GetSnapshotUri + httpx GET.

    Connection + GetProfiles + GetSnapshotUri + JPEG download all share the
    one ``budget`` budget via :func:`asyncio.wait_for`.
    """
    start = time.perf_counter()

    async def _do() -> bytes:
        wsdl_dir = resolve_onvif_wsdl_dir()
        # Lazy imports — onvif/httpx are optional at the module level.
        from onvif import ONVIFCamera  # type: ignore[import-untyped]

        cam = ONVIFCamera(ip, onvif_port, username, password, wsdl_dir=str(wsdl_dir))
        try:
            await cam.update_xaddrs()
            media = await cam.create_media_service()
            profiles = await media.GetProfiles()
            if not profiles:
                raise RuntimeError("GetProfiles returned empty list")
            token = getattr(profiles[0], "token", None)
            if not token:
                raise RuntimeError("first profile has no token")
            snap = await media.GetSnapshotUri({"ProfileToken": token})
            uri = getattr(snap, "Uri", None)
            if not uri:
                raise RuntimeError("GetSnapshotUri returned empty Uri")

            import httpx

            async with httpx.AsyncClient(timeout=budget) as client:
                resp = await client.get(uri, auth=(username, password))
                if resp.status_code == 401:
                    raise _AuthRejectedError("onvif", "HTTP 401 on snapshot URI")
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"snapshot URI returned HTTP {resp.status_code}"
                    )
                return resp.content
        finally:
            # onvif-zeep-async holds an internal aiohttp.ClientSession that
            # spews "Unclosed client session" warnings if it's GC'd without
            # cam.close(). Always close, even on the success path.
            with contextlib.suppress(Exception):
                await cam.close()

    try:
        payload = await asyncio.wait_for(_do(), timeout=budget)
    except _AuthRejectedError:
        raise
    except TimeoutError:
        return _TierResult(
            mechanism="onvif",
            status="fail",
            elapsed_ms=(time.perf_counter() - start) * 1000.0,
            detail=f"timeout after {budget:.2f}s",
        )
    except Exception as exc:
        msg = str(exc).lower()
        if "401" in msg or "unauthorized" in msg or "_auth_failed" in msg:
            raise _AuthRejectedError("onvif", str(exc)) from exc
        return _TierResult(
            mechanism="onvif",
            status="fail",
            elapsed_ms=(time.perf_counter() - start) * 1000.0,
            detail=f"{type(exc).__name__}: {exc}",
        )

    elapsed = (time.perf_counter() - start) * 1000.0
    if not _is_jpeg(payload):
        return _TierResult(
            mechanism="onvif",
            status="fail",
            elapsed_ms=elapsed,
            detail=f"non-JPEG payload from snapshot URI ({len(payload)} bytes)",
        )
    return _TierResult(
        mechanism="onvif",
        status="pass",
        elapsed_ms=elapsed,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Tier 3: ffmpeg single-frame from RTSP
# ---------------------------------------------------------------------------


def _tier3_ffmpeg(
    ip: str,
    username: str,
    password: str,
    *,
    ffmpeg_path: str,
    budget: float,
    rtsp_path: str = "stream1",
) -> _TierResult:
    """Pull a single frame from RTSP via an ffmpeg subprocess.

    ffmpeg-missing-on-PATH raises :class:`ConfigError` (exit 6, FR-11a.4) —
    NOT a tier-failure. The credentialed RTSP URL is built locally and only
    ever passed to subprocess on argv, never logged.
    """
    if shutil.which(ffmpeg_path) is None:
        raise ConfigError(
            f"snapshot tier 3 requires ffmpeg on PATH (looked for {ffmpeg_path!r})",
            hint=(
                "Install ffmpeg, set [ffmpeg] path in config, or run with "
                "--snapshot-budget ffmpeg=0 to disable the tier."
            ),
            extra={"missing_dependency": "ffmpeg"},
        )

    start = time.perf_counter()
    rtsp_url = build_rtsp_url(ip, username, password, path=rtsp_path)
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        out_path = Path(tmp.name)

    cmd = [
        ffmpeg_path,
        "-y",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-frames:v", "1",
        "-update", "1",
        "-f", "image2",
        str(out_path),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=budget, check=False
        )
    except subprocess.TimeoutExpired:
        out_path.unlink(missing_ok=True)
        return _TierResult(
            mechanism="ffmpeg",
            status="fail",
            elapsed_ms=(time.perf_counter() - start) * 1000.0,
            detail=f"timeout after {budget:.2f}s",
        )
    except FileNotFoundError as exc:  # ffmpeg disappeared between which() and run()
        out_path.unlink(missing_ok=True)
        raise ConfigError(
            f"ffmpeg vanished from PATH between detection and invocation: {exc}",
            hint="Reinstall ffmpeg or set [ffmpeg] path in config.",
            extra={"missing_dependency": "ffmpeg"},
        ) from exc

    elapsed = (time.perf_counter() - start) * 1000.0
    stderr = mask_url_credentials(
        proc.stderr.decode("utf-8", errors="replace")
    )

    # RTSP 401 surfaces in ffmpeg stderr as "401 Unauthorized" or
    # "Method DESCRIBE failed: 401 Unauthorized". Detect and short-circuit.
    if "401" in stderr and ("unauthorized" in stderr.lower() or "describe" in stderr.lower()):
        out_path.unlink(missing_ok=True)
        raise _AuthRejectedError("ffmpeg", "RTSP 401 Unauthorized")

    if proc.returncode != 0 or not out_path.exists():
        out_path.unlink(missing_ok=True)
        return _TierResult(
            mechanism="ffmpeg",
            status="fail",
            elapsed_ms=elapsed,
            detail=f"ffmpeg rc={proc.returncode}; stderr_tail={stderr[-200:]}",
        )

    payload = out_path.read_bytes()
    out_path.unlink(missing_ok=True)
    if not _is_jpeg(payload):
        return _TierResult(
            mechanism="ffmpeg",
            status="fail",
            elapsed_ms=elapsed,
            detail=f"non-JPEG payload from ffmpeg ({len(payload)} bytes)",
        )
    return _TierResult(
        mechanism="ffmpeg",
        status="pass",
        elapsed_ms=elapsed,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Click verb
# ---------------------------------------------------------------------------


@click.command("snapshot")
@click.argument("target", type=str)
@click.option(
    "--output",
    "output_path",
    type=str,
    required=True,
    help="Destination path for the JPEG. ``-`` writes binary bytes to stdout.",
)
@click.option(
    "--snapshot-budget",
    "budget_spec",
    type=str,
    default="",
    help=(
        "Override per-tier budgets in seconds: "
        "--snapshot-budget pytapo=N,onvif=N,ffmpeg=N. "
        "Sum SHALL NOT exceed --timeout (FR-11a.3)."
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
@click.pass_context
def snapshot_cmd(
    ctx: click.Context,
    *,
    target: str,
    output_path: str,
    budget_spec: str,
    onvif_port: int,
) -> None:
    """Pull a JPEG still image from a camera (three-mechanism fallback)."""
    state = ctx.obj
    mode: OutputMode = state["mode"]
    timeout = float(state.get("timeout") or 5.0)
    config_path = state.get("config_path")
    credential_source = state.get("credential_source")
    concurrency = state.get("concurrency")

    # FR-11d: --output - is incompatible with --json / --jsonl.
    if output_path == "-" and mode in (OutputMode.JSON, OutputMode.JSONL):
        click.echo(
            "error: --output - cannot be combined with --json or --jsonl",
            err=True,
        )
        sys.exit(EXIT_USAGE_ERROR)

    rc = _run_async(
        lambda: _run(
            target=target,
            output_path=output_path,
            budget_spec=budget_spec,
            onvif_port=onvif_port,
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
    output_path: str,
    budget_spec: str,
    onvif_port: int,
    mode: OutputMode,
    timeout: float,
    config_path: object,
    credential_source: object,
    concurrency: int | None = None,
) -> int:
    """Async core: load config, resolve creds, run the chain, write output."""

    cfg_path = Path(str(config_path)).expanduser() if config_path else None
    cfg = load_config(cfg_path)

    # FR-43d Phase 4a: snapshot honors @group with a per-member fan-out, but
    # only when --output is a writable path (NOT stdout). The path must
    # contain a ``{target}`` placeholder so each camera writes to a distinct
    # file -- silently clobbering N JPEGs into one path is the worst possible
    # behavior. The FR-43c carve-out for ``--output -`` (binary stdout x N
    # cameras = mess) preserves its exit-64.
    resolved_target_initial = target.lstrip("@") or target
    is_group = resolved_target_initial in cfg.groups
    if is_group:
        if output_path == "-":
            raise UsageError(
                "snapshot @group does not support --output - "
                "(binary stdout x N cameras)",
                hint=(
                    "Pass --output <path> with a {target} placeholder, e.g. "
                    "--output /tmp/snap-{target}.jpg, or invoke once per camera."
                ),
            )
        if "{target}" not in output_path:
            raise UsageError(
                "snapshot @group requires --output to contain a {target} placeholder",
                hint=(
                    "Example: --output /tmp/snap-{target}.jpg — the placeholder "
                    "is substituted with each camera's alias."
                ),
            )
        members = list(cfg.groups[resolved_target_initial])

        from tapo_cli.verbs._fanout import run_fanout

        async def _per_target(alias: str) -> tuple[int, dict[str, object]]:
            per_path = output_path.replace("{target}", alias)
            record = await _run_single(
                target=alias,
                output_path=per_path,
                budget_spec=budget_spec,
                onvif_port=onvif_port,
                mode=mode,
                timeout=timeout,
                config_path=config_path,
                credential_source=credential_source,
                cfg=cfg,
            )
            return 0, record

        return await run_fanout(
            members=members,
            per_target=_per_target,
            concurrency=concurrency or cfg.defaults.concurrency,
            mode=mode,
        )

    return await _run_single_emit(
        target=target,
        output_path=output_path,
        budget_spec=budget_spec,
        onvif_port=onvif_port,
        mode=mode,
        timeout=timeout,
        config_path=config_path,
        credential_source=credential_source,
        cfg=cfg,
    )


async def _run_single_emit(
    *,
    target: str,
    output_path: str,
    budget_spec: str,
    onvif_port: int,
    mode: OutputMode,
    timeout: float,
    config_path: object,
    credential_source: object,
    cfg: Config,
) -> int:
    """Single-target snapshot: chain + emit record on stdout. Returns exit code."""
    record = await _run_single(
        target=target,
        output_path=output_path,
        budget_spec=budget_spec,
        onvif_port=onvif_port,
        mode=mode,
        timeout=timeout,
        config_path=config_path,
        credential_source=credential_source,
        cfg=cfg,
    )
    # Single-target path emits via the FR-11b contract. ``--output -`` already
    # wrote bytes to stdout inside the chain; suppress the JSON record emission
    # there to avoid corrupting the binary stream.
    if output_path != "-":
        emit(record, mode, formatter=_record_to_text)
    return EXIT_SUCCESS


async def _run_single(
    *,
    target: str,
    output_path: str,
    budget_spec: str,
    onvif_port: int,
    mode: OutputMode,
    timeout: float,
    config_path: object,
    credential_source: object,
    cfg: Config,
) -> dict[str, object]:
    """Per-target snapshot core: chain attempt, write file, return record dict.

    Returns the success record. On all-tier-fail, propagates the underlying
    DeviceError. On auth-rejection, propagates AuthError. The fan-out path
    catches both via the standard B10 envelope.
    """
    # The original FR-49 / FR-43c reject — we only get here for single-target;
    # group reject was handled by ``_run`` before calling.
    resolved_target = target.lstrip("@") or target

    # Look up the device entry to get the IP. We also accept bare IPs.
    device = cfg.devices.get(resolved_target)
    if device is None:
        if not _looks_like_ipv4(resolved_target):
            raise UsageError(
                f"unknown alias: {resolved_target!r}",
                hint="Run `tapo-cli list` to see configured aliases.",
            )
        ip = resolved_target
    else:
        if not device.ip:
            raise UsageError(
                f"alias {resolved_target!r} has no ip in config",
                hint=f"Add ip = '<address>' under [devices.{resolved_target}].",
            )
        ip = device.ip

    # Resolve credentials. snapshot can use camera-account (preferred) OR
    # cloud-account — pytapo auth happens at tier 1, ONVIF at tier 2, RTSP
    # at tier 3, and all three accept the camera-account creds; cloud creds
    # also work on current C-series firmware. Use the standard control-plane
    # chain via wrapper, but only consume the resolved (user, pass) pair.
    from tapo_cli.credentials import resolve_control_plane

    src = (
        credential_source
        if credential_source in ("env", "file", "none")
        else None
    )
    cred = resolve_control_plane(cfg, alias=resolved_target, source=src)  # type: ignore[arg-type]
    if cred is None:
        raise AuthError(
            f"no credentials available for snapshot of {resolved_target!r}",
            target=resolved_target,
            hint=(
                "Configure either a camera_account_file (preferred) or "
                "cloud-account credentials at ~/.config/kasa-cli/credentials."
            ),
        )

    budget = _parse_budget_override(budget_spec, timeout)

    # ffmpeg path comes from config; default 'ffmpeg' on PATH.
    ffmpeg_path = cfg.ffmpeg.path

    chain_attempts: list[dict[str, object]] = []

    # ----- Tier 1: pytapo -----
    try:
        if budget.pytapo > 0:
            tier1 = await _tier1_pytapo(
                resolved_target, ip, cred.username, cred.password,
                budget=budget.pytapo,
            )
            chain_attempts.append(_attempt_dict(tier1))
            if tier1.status == "pass" and tier1.payload is not None:
                return _write_and_record(tier1, ip, resolved_target, output_path)
    except _AuthRejectedError as exc:
        raise AuthError(
            f"snapshot auth rejected at tier {exc.mechanism!r}: {exc.detail}",
            target=resolved_target,
            credential=cred.family,
            mechanism=exc.mechanism,
        ) from exc

    # ----- Tier 2: ONVIF -----
    try:
        if budget.onvif > 0:
            tier2 = await _tier2_onvif(
                ip, cred.username, cred.password,
                onvif_port=onvif_port, budget=budget.onvif,
            )
            chain_attempts.append(_attempt_dict(tier2))
            if tier2.status == "pass" and tier2.payload is not None:
                return _write_and_record(tier2, ip, resolved_target, output_path)
    except _AuthRejectedError as exc:
        raise AuthError(
            f"snapshot auth rejected at tier {exc.mechanism!r}: {exc.detail}",
            target=resolved_target,
            credential=cred.family,
            mechanism=exc.mechanism,
        ) from exc

    # ----- Tier 3: ffmpeg (sync, but on a worker thread for budget+isolation) -----
    if budget.ffmpeg > 0:
        try:
            tier3 = await asyncio.to_thread(
                _tier3_ffmpeg,
                ip, cred.username, cred.password,
                ffmpeg_path=ffmpeg_path,
                budget=budget.ffmpeg,
            )
        except _AuthRejectedError as exc:
            raise AuthError(
                f"snapshot auth rejected at tier {exc.mechanism!r}: {exc.detail}",
                target=resolved_target,
                credential=cred.family,
                mechanism=exc.mechanism,
            ) from exc
        chain_attempts.append(_attempt_dict(tier3))
        if tier3.status == "pass" and tier3.payload is not None:
            return _write_and_record(tier3, ip, resolved_target, output_path)

    # All tiers failed without auth-rejection → exit 1 (FR-11c).
    raise DeviceError(
        f"all snapshot mechanisms failed for {resolved_target!r}",
        target=resolved_target,
        extra={"attempts": chain_attempts},
    )


# ---------------------------------------------------------------------------
# Output emission
# ---------------------------------------------------------------------------


def _write_and_record(
    tier: _TierResult,
    ip: str,
    target_alias: str,
    output_path: str,
) -> dict[str, object]:
    """Write the JPEG and return the success record per FR-11b.

    Pulled out of the original ``_emit_success`` so the fan-out path can
    reuse the file-write + record-projection without the stdout emit step
    (the fan-out helper does its own JSONL emission).
    """
    assert tier.payload is not None  # invariant on a "pass" status

    if output_path == "-":
        # FR-11d: binary on stdout, regardless of --quiet (S15 carve-out).
        # Group fan-out forbids this combination at the dispatcher; we only
        # reach here in the single-target path.
        sys.stdout.buffer.write(tier.payload)
        sys.stdout.buffer.flush()
    else:
        out = Path(output_path).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(tier.payload)
        with contextlib.suppress(OSError):
            out.chmod(0o644)

    width, height = _jpeg_dimensions(tier.payload)
    return {
        "target": target_alias,
        "ip": ip,
        "mechanism": tier.mechanism,
        "bytes": len(tier.payload),
        "width": width,
        "height": height,
        "elapsed_ms": round(tier.elapsed_ms, 2),
        "ts": utc_now_rfc3339(),
        "output": output_path,
    }


def _record_to_text(record: object) -> str:
    """Compact one-line success summary for TEXT mode."""
    assert isinstance(record, dict)
    width = record.get("width")
    height = record.get("height")
    dims = f"{width}x{height}" if width and height else "?x?"
    return (
        f"snapshot ok target={record.get('target')} ip={record.get('ip')} "
        f"mechanism={record.get('mechanism')} bytes={record.get('bytes')} "
        f"dims={dims} elapsed_ms={record.get('elapsed_ms')}"
    )


def _attempt_dict(tier: _TierResult) -> dict[str, Any]:
    """Project a tier result onto a JSON-safe attempts-list entry."""
    return {
        "mechanism": tier.mechanism,
        "status": tier.status,
        "elapsed_ms": round(tier.elapsed_ms, 2),
        "detail": tier.detail,
    }


def _looks_like_ipv4(s: str) -> bool:
    parts = s.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


__all__ = ["snapshot_cmd"]
