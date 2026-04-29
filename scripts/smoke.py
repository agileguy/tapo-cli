#!/usr/bin/env python3
"""Phase 0 hardware-test harness for tapo-cli (SRD §16.0).

Runs seven probe mechanisms per camera against a JSON config, captures
per-mechanism pass/fail/elapsed, and writes a structured run report plus raw
fixtures (XML, JPEG bytes) to ``tests/fixtures/raw/``.

The script is deliberately standalone: it must work before any tapo-cli verbs
exist. It is the gate that decides whether the pinned pytapo SHA is fit for
purpose against real cameras.

Exit codes:
    0 — every camera had >=1 successful snapshot mechanism.
    1 — one or more cameras had zero successful snapshot mechanisms.
    6 — config file missing or malformed (SRD §11.1).
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

# Default config path per SRD §16.0 / smoke-test conventions.
DEFAULT_CONFIG = Path("~/.config/tapo-cli/smoke-cameras.json").expanduser()
DEFAULT_FIXTURES_DIR = Path("tests/fixtures")
DEFAULT_RAW_DIR = DEFAULT_FIXTURES_DIR / "raw"

# Mechanism order matches the SRD §16.0 probe list (a..g).
MECHANISMS = (
    "pytapo_getBasicInfo",
    "pytapo_getStreamURL",
    "pytapo_native_snapshot",
    "onvif_GetDeviceInformation",
    "onvif_GetProfiles",
    "onvif_GetSnapshotUri",
    "ffmpeg_rtsp_frame",
)

# Snapshot-tier mechanisms: at least one must pass per camera for the gate to clear.
SNAPSHOT_MECHANISMS = (
    "pytapo_native_snapshot",
    "onvif_GetSnapshotUri",
    "ffmpeg_rtsp_frame",
)

# Match basic-auth payload in any URL — RTSP, HTTP, or otherwise.
_AUTH_RE = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]+:[^/@\s]+@")


def mask_url_credentials(value: str) -> str:
    """Replace any ``scheme://user:pass@host`` payload with ``scheme://***:***@host``."""
    return _AUTH_RE.sub(lambda m: f"{m.group('scheme')}***:***@", value)


@dataclass
class MechanismResult:
    name: str
    status: str  # "pass" | "fail" | "skipped"
    elapsed_ms: float = 0.0
    detail: str | None = None
    fixture_path: str | None = None


@dataclass
class CameraResult:
    alias: str
    ip: str
    model: str
    mechanisms: list[MechanismResult] = field(default_factory=list)

    def by_name(self, name: str) -> MechanismResult | None:
        for m in self.mechanisms:
            if m.name == name:
                return m
        return None

    def snapshot_passed(self) -> bool:
        return any(
            (m := self.by_name(name)) is not None and m.status == "pass"
            for name in SNAPSHOT_MECHANISMS
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "ip": self.ip,
            "model": self.model,
            "snapshot_gate": "pass" if self.snapshot_passed() else "fail",
            "mechanisms": [
                {
                    "name": m.name,
                    "status": m.status,
                    "elapsed_ms": round(m.elapsed_ms, 2),
                    "detail": m.detail,
                    "fixture_path": m.fixture_path,
                }
                for m in self.mechanisms
            ],
        }


def load_config(path: Path) -> list[dict[str, Any]]:
    """Load the smoke config JSON. Raises FileNotFoundError or ValueError on failure."""
    if not path.exists():
        raise FileNotFoundError(f"smoke config not found: {path}")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError(f"expected a JSON array of cameras in {path}; got {type(data).__name__}")
    for i, cam in enumerate(data):
        for required in ("alias", "ip", "model", "username", "password"):
            if required not in cam:
                raise ValueError(f"camera {i} missing required field: {required}")
    return data


def _timed(fn):
    """Run ``fn``, return (result-or-exception, elapsed_ms)."""
    start = time.perf_counter()
    try:
        result = fn()
    except Exception as exc:
        return exc, (time.perf_counter() - start) * 1000.0
    return result, (time.perf_counter() - start) * 1000.0


async def _atimed(coro):
    start = time.perf_counter()
    try:
        result = await coro
    except Exception as exc:
        return exc, (time.perf_counter() - start) * 1000.0
    return result, (time.perf_counter() - start) * 1000.0


def probe_pytapo_basic_info(cam: dict[str, Any]) -> MechanismResult:
    from pytapo import Tapo

    def _run() -> dict[str, Any]:
        tapo = Tapo(cam["ip"], cam["username"], cam["password"])
        return tapo.getBasicInfo()

    out, elapsed = _timed(_run)
    if isinstance(out, Exception):
        return MechanismResult(
            name="pytapo_getBasicInfo",
            status="fail",
            elapsed_ms=elapsed,
            detail=f"{type(out).__name__}: {out}",
        )
    keys = ",".join(sorted((out or {}).get("device_info", {}).get("basic_info", {}).keys()))
    return MechanismResult(
        name="pytapo_getBasicInfo",
        status="pass",
        elapsed_ms=elapsed,
        detail=f"basic_info keys: {keys[:120]}",
    )


def probe_pytapo_stream_url(cam: dict[str, Any]) -> tuple[MechanismResult, str | None]:
    """Returns (result, raw_rtsp_url-or-None). Raw URL stays in-process; never printed."""
    from pytapo import Tapo

    def _run() -> str:
        tapo = Tapo(cam["ip"], cam["username"], cam["password"])
        return tapo.getStreamURL()

    out, elapsed = _timed(_run)
    if isinstance(out, Exception):
        return (
            MechanismResult(
                name="pytapo_getStreamURL",
                status="fail",
                elapsed_ms=elapsed,
                detail=f"{type(out).__name__}: {out}",
            ),
            None,
        )
    if not isinstance(out, str) or not out:
        return (
            MechanismResult(
                name="pytapo_getStreamURL",
                status="fail",
                elapsed_ms=elapsed,
                detail=f"unexpected return type: {type(out).__name__}",
            ),
            None,
        )
    return (
        MechanismResult(
            name="pytapo_getStreamURL",
            status="pass",
            elapsed_ms=elapsed,
            detail=f"url present (masked): {mask_url_credentials(out)}",
        ),
        out,
    )


def probe_pytapo_native_snapshot(cam: dict[str, Any], raw_dir: Path) -> MechanismResult:
    """pytapo at the pinned SHA has no first-class single-frame snapshot API.

    We attempt ``getMediaSession()`` as a best-effort liveness check on the
    encrypted media stream. If the call fails or the API is missing, the
    mechanism is marked ``skipped`` rather than ``fail`` — the snapshot gate
    passes via ONVIF GetSnapshotUri or ffmpeg-on-RTSP instead.
    """
    from pytapo import Tapo

    def _run() -> Any:
        tapo = Tapo(cam["ip"], cam["username"], cam["password"])
        if not hasattr(tapo, "getMediaSession"):
            raise AttributeError("pytapo at pinned SHA has no getMediaSession() — skip tier")
        return tapo.getMediaSession()

    out, elapsed = _timed(_run)
    if isinstance(out, AttributeError):
        return MechanismResult(
            name="pytapo_native_snapshot",
            status="skipped",
            elapsed_ms=elapsed,
            detail=str(out),
        )
    if isinstance(out, Exception):
        return MechanismResult(
            name="pytapo_native_snapshot",
            status="fail",
            elapsed_ms=elapsed,
            detail=f"{type(out).__name__}: {out}",
        )
    # We constructed a session object successfully. Phase 0 does not pull bytes
    # through the encrypted stream — that's a Phase 1 concern. Mark as `skipped`
    # with a clear reason; the operator's smoke gate is met by tiers (f) and (g).
    fixture = raw_dir / f"{cam['alias']}-pytapo.txt"
    fixture.write_text(
        "pytapo native snapshot tier: getMediaSession() returned a session object; "
        "single-frame extraction deferred to Phase 1.\n"
    )
    return MechanismResult(
        name="pytapo_native_snapshot",
        status="skipped",
        elapsed_ms=elapsed,
        detail="getMediaSession() ok; bytes-pull deferred to Phase 1",
        fixture_path=str(fixture),
    )


async def probe_onvif(cam: dict[str, Any], raw_dir: Path) -> list[MechanismResult]:
    """Run the three ONVIF mechanisms (d, e, f) sequentially; isolate failures."""
    from onvif import ONVIFCamera  # type: ignore[import-untyped]

    onvif_port = int(cam.get("onvif_port", 2020))
    results: list[MechanismResult] = []
    onvif: Any = None

    async def _connect() -> Any:
        c = ONVIFCamera(cam["ip"], onvif_port, cam["username"], cam["password"])
        await c.update_xaddrs()
        return c

    # (d) GetDeviceInformation
    out, elapsed = await _atimed(_connect())
    if isinstance(out, Exception):
        results.append(
            MechanismResult(
                name="onvif_GetDeviceInformation",
                status="fail",
                elapsed_ms=elapsed,
                detail=f"connect failed: {type(out).__name__}: {out}",
            )
        )
        # No connection = subsequent ONVIF probes can't run.
        for name in ("onvif_GetProfiles", "onvif_GetSnapshotUri"):
            results.append(
                MechanismResult(name=name, status="fail", detail="onvif connect failed upstream")
            )
        return results
    onvif = out

    async def _device_info() -> Any:
        return await onvif.devicemgmt.GetDeviceInformation()

    out, elapsed = await _atimed(_device_info())
    if isinstance(out, Exception):
        results.append(
            MechanismResult(
                name="onvif_GetDeviceInformation",
                status="fail",
                elapsed_ms=elapsed,
                detail=f"{type(out).__name__}: {out}",
            )
        )
    else:
        manufacturer = getattr(out, "Manufacturer", "?")
        model = getattr(out, "Model", "?")
        results.append(
            MechanismResult(
                name="onvif_GetDeviceInformation",
                status="pass",
                elapsed_ms=elapsed,
                detail=f"manufacturer={manufacturer} model={model}",
            )
        )

    # (e) GetProfiles
    media = await onvif.create_media_service()

    async def _profiles() -> Any:
        return await media.GetProfiles()

    out, elapsed = await _atimed(_profiles())
    profiles_xml = raw_dir / f"{cam['alias']}-getprofiles.xml"
    if isinstance(out, Exception):
        results.append(
            MechanismResult(
                name="onvif_GetProfiles",
                status="fail",
                elapsed_ms=elapsed,
                detail=f"{type(out).__name__}: {out}",
            )
        )
        first_profile_token: str | None = None
    else:
        # zeep returns objects; serializing the SOAP envelope itself requires a
        # transport hook. As a best-effort fixture, dump the python repr.
        profiles_xml.write_text(repr(out))
        first_profile_token = getattr(out[0], "token", None) if out else None
        results.append(
            MechanismResult(
                name="onvif_GetProfiles",
                status="pass",
                elapsed_ms=elapsed,
                detail=f"profiles={len(out)} first_token={first_profile_token}",
                fixture_path=str(profiles_xml),
            )
        )

    # (f) GetSnapshotUri + JPEG download
    if first_profile_token is None:
        results.append(
            MechanismResult(
                name="onvif_GetSnapshotUri",
                status="fail",
                detail="no profile token from GetProfiles",
            )
        )
        return results

    async def _snapuri() -> Any:
        return await media.GetSnapshotUri({"ProfileToken": first_profile_token})

    out, elapsed = await _atimed(_snapuri())
    snap_xml = raw_dir / f"{cam['alias']}-getsnapshoturi.xml"
    if isinstance(out, Exception):
        results.append(
            MechanismResult(
                name="onvif_GetSnapshotUri",
                status="fail",
                elapsed_ms=elapsed,
                detail=f"{type(out).__name__}: {out}",
            )
        )
        return results
    uri = getattr(out, "Uri", None)
    snap_xml.write_text(repr(out))
    if not uri:
        results.append(
            MechanismResult(
                name="onvif_GetSnapshotUri",
                status="fail",
                elapsed_ms=elapsed,
                detail="GetSnapshotUri returned empty Uri",
                fixture_path=str(snap_xml),
            )
        )
        return results

    # Download JPEG. Use httpx with basic auth fallback.
    import httpx

    out_jpeg = raw_dir / f"{cam['alias']}-onvif.jpg"

    async def _download() -> int:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(uri, auth=(cam["username"], cam["password"]))
            resp.raise_for_status()
            out_jpeg.write_bytes(resp.content)
            return len(resp.content)

    out, elapsed = await _atimed(_download())
    if isinstance(out, Exception):
        results.append(
            MechanismResult(
                name="onvif_GetSnapshotUri",
                status="fail",
                elapsed_ms=elapsed,
                detail=f"download failed: {type(out).__name__}: {out}",
                fixture_path=str(snap_xml),
            )
        )
    else:
        results.append(
            MechanismResult(
                name="onvif_GetSnapshotUri",
                status="pass",
                elapsed_ms=elapsed,
                detail=f"jpeg bytes={out} uri (masked)={mask_url_credentials(uri)}",
                fixture_path=str(out_jpeg),
            )
        )

    return results


def probe_ffmpeg_rtsp(cam: dict[str, Any], rtsp_url: str | None, raw_dir: Path) -> MechanismResult:
    """Pull a single frame from RTSP via ffmpeg subprocess.

    ``rtsp_url`` is the credentialed URL from pytapo's ``getStreamURL`` and is
    only ever passed to subprocess on argv — it is never logged or echoed.
    """
    if rtsp_url is None:
        return MechanismResult(
            name="ffmpeg_rtsp_frame",
            status="fail",
            detail="no RTSP URL available (pytapo_getStreamURL did not pass)",
        )
    out_jpeg = raw_dir / f"{cam['alias']}-ffmpeg.jpg"
    cmd = ["ffmpeg", "-y", "-i", rtsp_url, "-frames:v", "1", "-f", "image2", str(out_jpeg)]
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=30, check=False
        )
    except FileNotFoundError:
        return MechanismResult(
            name="ffmpeg_rtsp_frame",
            status="fail",
            elapsed_ms=(time.perf_counter() - start) * 1000.0,
            detail="ffmpeg not on PATH",
        )
    except subprocess.TimeoutExpired:
        return MechanismResult(
            name="ffmpeg_rtsp_frame",
            status="fail",
            elapsed_ms=(time.perf_counter() - start) * 1000.0,
            detail="ffmpeg timeout after 30s",
        )
    elapsed = (time.perf_counter() - start) * 1000.0
    # ffmpeg stderr can echo the URL back verbatim — mask before reporting.
    stderr = mask_url_credentials(proc.stderr.decode("utf-8", errors="replace"))[:400]
    if proc.returncode != 0 or not out_jpeg.exists():
        return MechanismResult(
            name="ffmpeg_rtsp_frame",
            status="fail",
            elapsed_ms=elapsed,
            detail=f"ffmpeg rc={proc.returncode}; stderr_tail={stderr[-200:]}",
        )
    return MechanismResult(
        name="ffmpeg_rtsp_frame",
        status="pass",
        elapsed_ms=elapsed,
        detail=f"jpeg bytes={out_jpeg.stat().st_size}",
        fixture_path=str(out_jpeg),
    )


async def run_camera(cam: dict[str, Any], raw_dir: Path) -> CameraResult:
    result = CameraResult(alias=cam["alias"], ip=cam["ip"], model=cam["model"])

    # (a) pytapo getBasicInfo
    result.mechanisms.append(probe_pytapo_basic_info(cam))

    # (b) pytapo getStreamURL — keeps raw URL in-process for tier (g).
    stream_result, rtsp_url = probe_pytapo_stream_url(cam)
    result.mechanisms.append(stream_result)

    # (c) pytapo native snapshot — best-effort.
    result.mechanisms.append(probe_pytapo_native_snapshot(cam, raw_dir))

    # (d, e, f) ONVIF mechanisms.
    result.mechanisms.extend(await probe_onvif(cam, raw_dir))

    # (g) ffmpeg single-frame from RTSP.
    result.mechanisms.append(probe_ffmpeg_rtsp(cam, rtsp_url, raw_dir))

    return result


def render_text_report(results: list[CameraResult]) -> str:
    lines: list[str] = []
    for cam in results:
        gate = "PASS" if cam.snapshot_passed() else "FAIL"
        lines.append(f"== {cam.alias} ({cam.model} @ {cam.ip}) — gate: {gate}")
        for m in cam.mechanisms:
            tag = {"pass": "[PASS]", "fail": "[FAIL]", "skipped": "[SKIP]"}.get(m.status, "[????]")
            detail = (m.detail or "")[:120]
            lines.append(f"  {tag} {m.name:32s} {m.elapsed_ms:7.1f}ms  {detail}")
        lines.append("")
    return "\n".join(lines)


def write_report(results: list[CameraResult], fixtures_dir: Path) -> Path:
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = fixtures_dir / f"smoke-report-{stamp}.json"
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "cameras": [c.to_dict() for c in results],
        "gate": "pass" if all(c.snapshot_passed() for c in results) else "fail",
    }
    path.write_text(json.dumps(payload, indent=2))
    return path


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--cameras",
    "cameras_path",
    type=click.Path(path_type=Path),
    default=str(DEFAULT_CONFIG),
    show_default=True,
    help="Path to smoke-cameras.json config.",
)
@click.option(
    "--fixtures-dir",
    type=click.Path(path_type=Path),
    default=str(DEFAULT_FIXTURES_DIR),
    show_default=True,
    help="Where to write smoke-report-*.json (raw fixtures go to <dir>/raw/).",
)
@click.option(
    "--json",
    "json_mode",
    is_flag=True,
    help="Emit one JSON object per camera on stdout.",
)
def main(cameras_path: Path, fixtures_dir: Path, json_mode: bool) -> None:
    """Phase 0 hardware-test harness — see SRD §16.0."""
    try:
        cameras = load_config(cameras_path)
    except FileNotFoundError as exc:
        click.echo(f"error: {exc}", err=True)
        click.echo(
            "hint: create the file with a JSON array of "
            "{alias, ip, model, username, password[, onvif_port]} entries.",
            err=True,
        )
        sys.exit(6)
    except ValueError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(6)

    raw_dir = Path(fixtures_dir) / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    async def _run_all() -> list[CameraResult]:
        out: list[CameraResult] = []
        for cam in cameras:
            try:
                out.append(await run_camera(cam, raw_dir))
            except Exception as exc:
                click.echo(
                    f"error: unhandled exception for {cam.get('alias', '?')}: {exc}", err=True
                )
                traceback.print_exc(file=sys.stderr)
                cam_result = CameraResult(
                    alias=cam.get("alias", "?"),
                    ip=cam.get("ip", "?"),
                    model=cam.get("model", "?"),
                )
                cam_result.mechanisms.append(
                    MechanismResult(
                        name="harness", status="fail", detail=f"{type(exc).__name__}: {exc}"
                    )
                )
                out.append(cam_result)
        return out

    results = asyncio.run(_run_all())

    if json_mode:
        for cam in results:
            click.echo(json.dumps(cam.to_dict()))
    else:
        click.echo(render_text_report(results))

    report_path = write_report(results, Path(fixtures_dir))
    click.echo(f"report: {report_path}", err=True)

    sys.exit(0 if all(c.snapshot_passed() for c in results) else 1)


if __name__ == "__main__":
    main()
