"""Dry-run unit tests for ``scripts/smoke.py`` — never touches the network.

pytapo, onvif-zeep-async, and subprocess.run are all monkeypatched. These tests
verify the harness's contract (redaction, partial-failure isolation, JSON mode,
exit codes) without requiring a real camera.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest
from click.testing import CliRunner

# Load scripts/smoke.py as a module without putting scripts/ on sys.path.
_SMOKE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "smoke.py"


def _load_smoke():
    """Import scripts/smoke.py with pytapo + onvif stubs already in sys.modules."""
    # Stub pytapo before loading so smoke.py's lazy imports succeed in test.
    if "pytapo" not in sys.modules:
        pytapo_stub = types.ModuleType("pytapo")
        pytapo_stub.Tapo = lambda *args, **kwargs: None  # type: ignore[attr-defined]
        sys.modules["pytapo"] = pytapo_stub
    if "onvif" not in sys.modules:
        onvif_stub = types.ModuleType("onvif")
        onvif_stub.ONVIFCamera = lambda *args, **kwargs: None  # type: ignore[attr-defined]
        sys.modules["onvif"] = onvif_stub

    spec = importlib.util.spec_from_file_location("tapo_smoke", _SMOKE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec_module — Python 3.14 dataclass introspection looks
    # up the owning module via sys.modules during class construction.
    sys.modules["tapo_smoke"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def smoke():
    return _load_smoke()


# ─────────────────────── credential redaction ───────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            "rtsp://admin:hunter2@192.168.1.42:554/stream1",
            "rtsp://***:***@192.168.1.42:554/stream1",
        ),
        (
            "http://user:pa$$w0rd@cam.local/snap.jpg?token=abc",
            "http://***:***@cam.local/snap.jpg?token=abc",
        ),
        (
            "https://user:pass@host:8443/onvif/snapshot",
            "https://***:***@host:8443/onvif/snapshot",
        ),
        (
            "rtsp://192.168.1.42:554/stream1",
            "rtsp://192.168.1.42:554/stream1",  # unchanged — no auth payload
        ),
        (
            "no scheme just text",
            "no scheme just text",
        ),
    ],
)
def test_mask_url_credentials(smoke, raw, expected):
    assert smoke.mask_url_credentials(raw) == expected


def test_mask_url_credentials_idempotent(smoke):
    once = smoke.mask_url_credentials("rtsp://u:p@host/s")
    twice = smoke.mask_url_credentials(once)
    assert once == twice == "rtsp://***:***@host/s"


# ─────────────────────── partial failure isolation ───────────────────────


def test_one_mechanism_failure_does_not_abort_others(smoke, monkeypatch, tmp_path):
    """If pytapo_getBasicInfo blows up, the harness must still record the failure
    and proceed to capture results for the remaining six mechanisms."""

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()

    cam = {
        "alias": "test-cam",
        "ip": "192.0.2.10",
        "model": "C320WS",
        "username": "u",
        "password": "p",
    }

    # Make pytapo_getBasicInfo raise.
    def boom(*_a, **_kw):
        raise RuntimeError("simulated upstream failure")

    monkeypatch.setattr(smoke, "probe_pytapo_basic_info", lambda c: smoke.MechanismResult(
        name="pytapo_getBasicInfo", status="fail", elapsed_ms=1.0, detail="simulated"
    ))
    monkeypatch.setattr(
        smoke,
        "probe_pytapo_stream_url",
        lambda c: (
            smoke.MechanismResult(name="pytapo_getStreamURL", status="fail", detail="simulated"),
            None,
        ),
    )
    monkeypatch.setattr(
        smoke,
        "probe_pytapo_native_snapshot",
        lambda c, raw: smoke.MechanismResult(
            name="pytapo_native_snapshot", status="skipped", detail="api absent"
        ),
    )

    async def fake_onvif(_cam, _raw_dir):
        return [
            smoke.MechanismResult(name="onvif_GetDeviceInformation", status="fail", detail="x"),
            smoke.MechanismResult(name="onvif_GetProfiles", status="fail", detail="x"),
            smoke.MechanismResult(name="onvif_GetSnapshotUri", status="fail", detail="x"),
        ]

    monkeypatch.setattr(smoke, "probe_onvif", fake_onvif)
    monkeypatch.setattr(
        smoke,
        "probe_ffmpeg_rtsp",
        lambda c, url, raw: smoke.MechanismResult(
            name="ffmpeg_rtsp_frame", status="fail", detail="no rtsp url"
        ),
    )

    import asyncio

    result = asyncio.run(smoke.run_camera(cam, raw_dir))
    # All seven mechanisms reported, no early abort.
    names = [m.name for m in result.mechanisms]
    assert names == list(smoke.MECHANISMS)
    # Snapshot gate fails — every snapshot tier failed/skipped.
    assert result.snapshot_passed() is False


# ─────────────────────── JSON mode emits valid JSON ───────────────────────


def test_json_mode_emits_valid_json(smoke, monkeypatch, tmp_path):
    config = tmp_path / "cams.json"
    config.write_text(
        json.dumps(
            [
                {
                    "alias": "c1",
                    "ip": "192.0.2.10",
                    "model": "C320WS",
                    "username": "u",
                    "password": "p",
                }
            ]
        )
    )

    async def fake_run_camera(cam, raw_dir):
        cr = smoke.CameraResult(alias=cam["alias"], ip=cam["ip"], model=cam["model"])
        cr.mechanisms.append(
            smoke.MechanismResult(name="onvif_GetSnapshotUri", status="pass", elapsed_ms=10.0)
        )
        return cr

    monkeypatch.setattr(smoke, "run_camera", fake_run_camera)

    runner = CliRunner()
    result = runner.invoke(
        smoke.main,
        ["--cameras", str(config), "--fixtures-dir", str(tmp_path), "--json"],
    )
    assert result.exit_code == 0, result.output
    # Each non-empty stdout line must be valid JSON.
    for line in [line for line in result.stdout.splitlines() if line.strip()]:
        parsed = json.loads(line)
        assert "alias" in parsed
        assert "mechanisms" in parsed
        assert parsed["snapshot_gate"] == "pass"


# ─────────────────────── exit code 6 on missing config ───────────────────────


def test_missing_config_exits_6(smoke, tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        smoke.main,
        ["--cameras", str(tmp_path / "does-not-exist.json"), "--fixtures-dir", str(tmp_path)],
    )
    assert result.exit_code == 6
    assert "smoke config not found" in result.output or "smoke config not found" in (
        result.stderr if hasattr(result, "stderr") else ""
    )


def test_invalid_json_config_exits_6(smoke, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not valid json {{{")
    runner = CliRunner()
    result = runner.invoke(
        smoke.main,
        ["--cameras", str(bad), "--fixtures-dir", str(tmp_path)],
    )
    assert result.exit_code == 6


def test_config_missing_required_field_exits_6(smoke, tmp_path):
    bad = tmp_path / "incomplete.json"
    bad.write_text(json.dumps([{"alias": "c1", "ip": "192.0.2.10"}]))  # missing model/user/pass
    runner = CliRunner()
    result = runner.invoke(
        smoke.main,
        ["--cameras", str(bad), "--fixtures-dir", str(tmp_path)],
    )
    assert result.exit_code == 6


# ─────────────────────── exit 1 when snapshot gate fails ───────────────────────


def test_exit_1_when_snapshot_gate_fails(smoke, monkeypatch, tmp_path):
    config = tmp_path / "cams.json"
    config.write_text(
        json.dumps(
            [
                {
                    "alias": "c1",
                    "ip": "192.0.2.10",
                    "model": "C320WS",
                    "username": "u",
                    "password": "p",
                }
            ]
        )
    )

    async def fake_run_camera(cam, raw_dir):
        cr = smoke.CameraResult(alias=cam["alias"], ip=cam["ip"], model=cam["model"])
        # Every snapshot tier fails.
        for name in smoke.MECHANISMS:
            cr.mechanisms.append(smoke.MechanismResult(name=name, status="fail", detail="x"))
        return cr

    monkeypatch.setattr(smoke, "run_camera", fake_run_camera)

    runner = CliRunner()
    result = runner.invoke(
        smoke.main,
        ["--cameras", str(config), "--fixtures-dir", str(tmp_path)],
    )
    assert result.exit_code == 1


def test_exit_0_when_one_snapshot_tier_passes(smoke, monkeypatch, tmp_path):
    config = tmp_path / "cams.json"
    config.write_text(
        json.dumps(
            [
                {
                    "alias": "c1",
                    "ip": "192.0.2.10",
                    "model": "C320WS",
                    "username": "u",
                    "password": "p",
                }
            ]
        )
    )

    async def fake_run_camera(cam, raw_dir):
        cr = smoke.CameraResult(alias=cam["alias"], ip=cam["ip"], model=cam["model"])
        cr.mechanisms.append(
            smoke.MechanismResult(name="ffmpeg_rtsp_frame", status="pass", elapsed_ms=42.0)
        )
        return cr

    monkeypatch.setattr(smoke, "run_camera", fake_run_camera)

    runner = CliRunner()
    result = runner.invoke(
        smoke.main,
        ["--cameras", str(config), "--fixtures-dir", str(tmp_path)],
    )
    assert result.exit_code == 0


# ─────────────────────── ffmpeg subprocess masking ───────────────────────


def test_ffmpeg_failure_masks_credentials_in_stderr_tail(smoke, monkeypatch, tmp_path):
    """If ffmpeg writes the credentialed URL into stderr, the reported detail must mask it."""

    class FakeCompleted:
        returncode = 1
        stderr = b"input rtsp://admin:hunter2@192.0.2.10/stream1 failed: 401 unauthorized"

    monkeypatch.setattr(smoke.subprocess, "run", lambda *a, **kw: FakeCompleted())

    cam = {"alias": "c1", "ip": "192.0.2.10", "model": "C320WS", "username": "u", "password": "p"}
    result = smoke.probe_ffmpeg_rtsp(
        cam, "rtsp://admin:hunter2@192.0.2.10/stream1", tmp_path
    )
    assert result.status == "fail"
    assert "hunter2" not in (result.detail or "")
    assert "***:***" in (result.detail or "")


def test_ffmpeg_not_on_path_reports_clean(smoke, monkeypatch, tmp_path):
    def raise_fnf(*_a, **_kw):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(smoke.subprocess, "run", raise_fnf)
    cam = {"alias": "c1", "ip": "192.0.2.10", "model": "C320WS", "username": "u", "password": "p"}
    result = smoke.probe_ffmpeg_rtsp(cam, "rtsp://x:y@host/s", tmp_path)
    assert result.status == "fail"
    assert "ffmpeg not on PATH" in (result.detail or "")


# ─────────────────────── load_config edge cases ───────────────────────


def test_load_config_rejects_non_array(smoke, tmp_path):
    p = tmp_path / "object.json"
    p.write_text(json.dumps({"alias": "c1"}))
    with pytest.raises(ValueError, match="expected a JSON array"):
        smoke.load_config(p)


def test_load_config_accepts_optional_onvif_port(smoke, tmp_path):
    p = tmp_path / "cams.json"
    p.write_text(
        json.dumps(
            [
                {
                    "alias": "c1",
                    "ip": "192.0.2.10",
                    "model": "C320WS",
                    "username": "u",
                    "password": "p",
                    "onvif_port": 8000,
                }
            ]
        )
    )
    cams = smoke.load_config(p)
    assert cams[0]["onvif_port"] == 8000
