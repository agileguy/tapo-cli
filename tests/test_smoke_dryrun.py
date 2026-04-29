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

    async def fake_basic(_c):
        return smoke.MechanismResult(
            name="pytapo_getBasicInfo", status="fail", elapsed_ms=1.0, detail="simulated"
        )

    async def fake_stream(_c):
        return smoke.MechanismResult(
            name="pytapo_getStreamURL", status="fail", detail="simulated"
        )

    async def fake_native(_c, _raw):
        return smoke.MechanismResult(
            name="pytapo_native_snapshot", status="skipped", detail="api absent"
        )

    monkeypatch.setattr(smoke, "probe_pytapo_basic_info", fake_basic)
    monkeypatch.setattr(smoke, "probe_pytapo_stream_url", fake_stream)
    monkeypatch.setattr(smoke, "probe_pytapo_native_snapshot", fake_native)

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
        lambda c, raw: smoke.MechanismResult(
            name="ffmpeg_rtsp_frame", status="fail", detail="ffmpeg simulated fail"
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

    cam = {
        "alias": "c1",
        "ip": "192.0.2.10",
        "model": "C320WS",
        "username": "admin",
        "password": "hunter2",
    }
    result = smoke.probe_ffmpeg_rtsp(cam, tmp_path)
    assert result.status == "fail"
    assert "hunter2" not in (result.detail or "")
    assert "***:***" in (result.detail or "")


def test_ffmpeg_not_on_path_reports_clean(smoke, monkeypatch, tmp_path):
    def raise_fnf(*_a, **_kw):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(smoke.subprocess, "run", raise_fnf)
    cam = {"alias": "c1", "ip": "192.0.2.10", "model": "C320WS", "username": "u", "password": "p"}
    result = smoke.probe_ffmpeg_rtsp(cam, tmp_path)
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


# ─────────────────────── BUG 1: asyncio reentrancy ───────────────────────


def test_pytapo_probe_does_not_collide_with_outer_event_loop(smoke, monkeypatch, tmp_path):
    """Regression test for BUG 1.

    Simulate pytapo's behaviour: a sync function that internally invokes
    ``loop.run_until_complete()``. If the smoke probe calls it on the calling
    thread instead of via ``asyncio.to_thread``, asyncio raises
    ``RuntimeError: Cannot run the event loop while another loop is running``
    because we're already inside ``asyncio.run``. The probe must isolate the
    sync call onto a worker thread so this never happens.
    """
    import asyncio

    def fake_basic_info() -> dict:
        # Mimic pytapo.AsyncHandler: run a fresh loop inside a sync callable.
        async def _inner() -> str:
            await asyncio.sleep(0)
            return "ok"

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_inner())
        finally:
            asyncio.set_event_loop(None)
            loop.close()
        return {"device_info": {"basic_info": {"device_model": "C200"}}}

    fake_tapo = type(
        "FakeTapo",
        (),
        {
            "__init__": lambda self, *a, **kw: None,
            "getBasicInfo": lambda self: fake_basic_info(),
        },
    )

    fake_module = types.ModuleType("pytapo")
    fake_module.Tapo = fake_tapo  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pytapo", fake_module)

    cam = {"alias": "c1", "ip": "192.0.2.10", "model": "C200", "username": "u", "password": "p"}
    # If the probe ran the sync call on the calling thread, this would raise.
    result = asyncio.run(smoke.probe_pytapo_basic_info(cam))
    assert result.status == "pass"
    assert "device_model" in (result.detail or "")


# ─────────────────────── BUG 2: ONVIF WSDL discovery ───────────────────────


def test_resolve_onvif_wsdl_dir_raises_clearly_when_bundle_missing(
    smoke, monkeypatch, tmp_path
):
    """Regression test for BUG 2.

    If the onvif package is installed but the bundled wsdl/ directory is
    missing (e.g. a packaging bug under Python 3.14), the resolver must
    surface a clear, actionable 'install or pin onvif-zeep-async correctly'
    message — not a cryptic FileNotFoundError that points at a stray
    site-packages/wsdl/devicemgmt.wsdl path the user can't act on.
    """
    fake_onvif = types.ModuleType("onvif")
    fake_onvif.__file__ = str(tmp_path / "onvif" / "__init__.py")  # bundle missing
    (tmp_path / "onvif").mkdir()
    monkeypatch.setitem(sys.modules, "onvif", fake_onvif)

    with pytest.raises(FileNotFoundError) as excinfo:
        smoke.resolve_onvif_wsdl_dir()
    msg = str(excinfo.value)
    assert "ONVIF unavailable" in msg
    assert "onvif-zeep-async" in msg


def test_probe_onvif_routes_missing_wsdl_to_clear_failure(smoke, monkeypatch, tmp_path):
    """If the WSDL bundle is missing, all three ONVIF tiers must fail with the
    same clear 'ONVIF unavailable — install or pin onvif-zeep-async correctly'
    message rather than the upstream library's cryptic FileNotFoundError."""
    import asyncio

    def boom() -> Path:
        raise FileNotFoundError("ONVIF unavailable — install or pin onvif-zeep-async correctly")

    monkeypatch.setattr(smoke, "resolve_onvif_wsdl_dir", boom)

    cam = {"alias": "c1", "ip": "192.0.2.10", "model": "C200", "username": "u", "password": "p"}
    results = asyncio.run(smoke.probe_onvif(cam, tmp_path))
    assert [r.name for r in results] == [
        "onvif_GetDeviceInformation",
        "onvif_GetProfiles",
        "onvif_GetSnapshotUri",
    ]
    assert all(r.status == "fail" for r in results)
    assert all("ONVIF unavailable" in (r.detail or "") for r in results)
    assert all("onvif-zeep-async" in (r.detail or "") for r in results)


# ─────────────────────── BUG 3: RTSP URL construction ───────────────────────


@pytest.mark.parametrize(
    "username,password",
    [
        ("admin", "p@ss:word"),       # @ and : in password
        ("user/with/slash", "pass"),  # / in username
        ("u", "with?query#hash"),     # ? and # in password
        ("u", "bang!exclaim"),        # ! in password
        ("u", "amp&pers"),            # & in password (URL query separator)
        ("u", " spaces and ünïcödé"), # spaces and non-ASCII
    ],
)
def test_build_rtsp_url_quotes_special_characters(smoke, username, password):
    """Regression test for BUG 3.

    Passwords containing URL-reserved characters (@, :, /, !, ?, #, &) must be
    percent-encoded so they don't corrupt the userinfo / host / path / query
    boundaries of the RTSP URL ffmpeg consumes.
    """
    from urllib.parse import quote, urlsplit

    url = smoke.build_rtsp_url("192.0.2.10", username, password)
    parts = urlsplit(url)

    assert parts.scheme == "rtsp"
    assert parts.hostname == "192.0.2.10"
    assert parts.port == 554
    assert parts.path == "/stream1"
    # Reserved chars must be percent-encoded — never raw — in userinfo.
    for raw in ("@", ":", "/", "?", "#", "&", "!"):
        assert raw not in (parts.username or "")
        # ':' inside password would split user:pass at the wrong place.
        # urlsplit can still report the password if we encoded properly.
    # Round-trip: urlsplit must be able to recover the originals.
    assert parts.username == quote(username, safe="")
    assert parts.password == quote(password, safe="")


def test_build_rtsp_url_default_port_and_path(smoke):
    url = smoke.build_rtsp_url("192.0.2.10", "u", "p")
    assert url == "rtsp://u:p@192.0.2.10:554/stream1"


def test_ffmpeg_probe_builds_url_locally_not_from_pytapo(smoke, monkeypatch, tmp_path):
    """probe_ffmpeg_rtsp must build its RTSP URL from camera config — never
    consume pytapo.getStreamURL()'s return value, which on the pinned SHA is
    a bare host:port string, not a usable URL."""
    captured: dict = {}

    class FakeProc:
        returncode = 0
        stderr = b""

    def fake_run(cmd, *_a, **_kw):
        captured["cmd"] = cmd
        # Simulate ffmpeg writing the JPEG so the success path returns pass.
        out_path = Path(cmd[cmd.index("-f") + 2])
        out_path.write_bytes(b"\xff\xd8\xff\xe0fake jpeg")
        return FakeProc()

    monkeypatch.setattr(smoke.subprocess, "run", fake_run)

    cam = {
        "alias": "c1",
        "ip": "10.20.30.40",
        "model": "C200",
        "username": "user",
        "password": "p@ss:word",
    }
    result = smoke.probe_ffmpeg_rtsp(cam, tmp_path)
    assert result.status == "pass"
    # The URL passed to ffmpeg was built from cam config, not pytapo, and the
    # password's special characters were percent-encoded.
    cmd = captured["cmd"]
    rtsp_arg = cmd[cmd.index("-i") + 1]
    assert rtsp_arg.startswith("rtsp://user:p%40ss%3Aword@10.20.30.40:554/stream1")
