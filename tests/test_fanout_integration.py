"""Cross-verb fan-out integration tests (Phase 4a, FR-43d / FR-56).

Phase 4a generalizes ``_fanout.run_fanout`` from ptz-only to every verb in
FR-43d's enumeration. This file pins the per-verb integration contract:
each verb, when given an ``@group`` target, MUST emit one JSONL line per
member in resolved-alias-list order with the FR-44a / B10 envelope.

Coverage: every verb in FR-43d (info, privacy, led, night-vision,
motion enable/disable/status/history, alarm, audio, osd, preset, reboot,
snapshot, set) plus the carve-outs (stream, record, snapshot --output -).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from tapo_cli.cli import main
from tapo_cli.wrapper import TapoConnection, TapoTarget


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TAPO_CLI_CONFIG", raising=False)
    monkeypatch.delenv("TAPO_USERNAME", raising=False)
    monkeypatch.delenv("TAPO_PASSWORD", raising=False)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _multi_cam_cfg(tmp_path: Path, *, model: str = "C200") -> Path:
    """Three-camera config with a ``cams`` group enumerating all three."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f'[devices.alpha]\nip = "192.168.1.11"\nmac = "AA:BB:CC:DD:EE:01"\nmodel = "{model}"\n'
        f'\n[devices.beta]\nip = "192.168.1.12"\nmac = "AA:BB:CC:DD:EE:02"\nmodel = "{model}"\n'
        f'\n[devices.gamma]\nip = "192.168.1.13"\nmac = "AA:BB:CC:DD:EE:03"\nmodel = "{model}"\n'
        f'\n[groups]\ncams = ["alpha", "beta", "gamma"]\n',
        encoding="utf-8",
    )
    return cfg_path


class _UniversalFakeTapo:
    """A union-ish fake covering every pytapo surface tapped by the migrated verbs."""

    def __init__(self, *, model: str = "C200") -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.model = model
        self.basic_info = {
            "device_info": {"basic_info": {"device_model": model}}
        }

    # ----- info -----
    def getBasicInfo(self) -> dict[str, Any]:  # noqa: N802
        return self.basic_info

    # ----- privacy -----
    def setPrivacyMode(self, enabled: bool) -> None:  # noqa: N802
        self.calls.append(("setPrivacyMode", (enabled,)))

    def getPrivacyMode(self) -> dict[str, str]:  # noqa: N802
        return {"enabled": "off"}

    # ----- led -----
    def setLEDEnabled(self, enabled: bool) -> None:  # noqa: N802
        self.calls.append(("setLEDEnabled", (enabled,)))

    def getLED(self) -> dict[str, str]:  # noqa: N802
        return {"enabled": "off"}

    # ----- night-vision -----
    def setDayNightMode(self, mode: str, chn_id=None) -> None:  # noqa: N802
        self.calls.append(("setDayNightMode", (mode,)))

    def getDayNightMode(self, chn_id=None) -> str:  # noqa: N802
        return "auto"

    # ----- motion -----
    def setMotionDetection(self, enabled=None, sensitivity=False, chn_id=None) -> None:  # noqa: N802
        self.calls.append(("setMotionDetection", (enabled,)))

    def getMotionDetection(self) -> dict[str, str]:  # noqa: N802
        return {"enabled": "off", "digital_sensitivity": "60"}

    def getEvents(self, startTime=False, endTime=False) -> list[dict]:  # noqa: N802, N803
        return []

    # ----- alarm -----
    def setAlarm(self, enabled, soundEnabled=True, lightEnabled=True, **kwargs) -> None:  # noqa: N802, N803
        self.calls.append(("setAlarm", (enabled,)))

    def getAlarm(self) -> dict[str, Any]:  # noqa: N802
        return {"enabled": "off", "alarm_mode": ["sound"]}

    def startManualAlarm(self) -> None:  # noqa: N802
        self.calls.append(("startManualAlarm", ()))

    # ----- audio -----
    def setSpeakerVolume(self, volume: int) -> None:  # noqa: N802
        self.calls.append(("setSpeakerVolume", (volume,)))

    # ----- osd -----
    def getOsd(self) -> dict[str, Any]:  # noqa: N802
        return {"dateEnabled": True, "labelEnabled": False, "label": ""}

    def setOsd(self, *args, **kwargs) -> None:  # noqa: N802
        self.calls.append(("setOsd", args))

    # ----- preset (PTZ-gated; we use C225 fixture for those tests) -----
    def getPresets(self) -> dict[str, str]:  # noqa: N802
        return {"1": "home", "2": "patio"}

    def setPreset(self, presetID, retry=False) -> None:  # noqa: N802, N803
        self.calls.append(("setPreset", (presetID,)))

    # ----- reboot -----
    def reboot(self, delay=None) -> dict[str, int]:
        self.calls.append(("reboot", (delay,)))
        return {"error_code": 0}

    # ----- set -----
    def setImageFlipVertical(self, enable: bool, chn_id=None) -> None:  # noqa: N802
        self.calls.append(("setImageFlipVertical", (enable,)))

    def setTimezone(self, timezone, zoneID, timingMode="ntp") -> None:  # noqa: N802, N803
        self.calls.append(("setTimezone", (timezone, zoneID)))


def _patch_universal_connect(
    monkeypatch, model: str = "C200"
) -> dict[str, _UniversalFakeTapo]:
    """Patch wrapper.connect to return a per-alias UniversalFakeTapo. Returns
    the alias→fake mapping so tests can assert on per-camera calls."""
    tapos: dict[str, _UniversalFakeTapo] = {}

    async def _fake_connect(cfg, target, *, credential_source=None, timeout=5.0):
        if target not in tapos:
            tapos[target] = _UniversalFakeTapo(model=model)
        tapo = tapos[target]
        return TapoConnection(
            tapo=tapo,
            target=TapoTarget(alias=target, ip=f"192.168.1.{10 + len(target)}"),
            credential_family="camera_account",
        )

    monkeypatch.setattr("tapo_cli.wrapper.connect", _fake_connect)
    return tapos


def _parse_lines(out: str) -> list[dict[str, Any]]:
    """Parse JSONL output, dropping blanks."""
    return [json.loads(line) for line in out.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Per-verb integration matrix (FR-43d enumeration, in SRD order)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv,expected_call",
    [
        # info — fan-out emits 3 lines, no pytapo-mutation calls
        (["info", "@cams"], None),
        # privacy
        (["privacy", "@cams", "enable"], ("setPrivacyMode", (True,))),
        (["privacy", "@cams", "disable"], ("setPrivacyMode", (False,))),
        (["privacy", "@cams", "status"], None),
        # led
        (["led", "@cams", "on"], ("setLEDEnabled", (True,))),
        (["led", "@cams", "off"], ("setLEDEnabled", (False,))),
        (["led", "@cams", "status"], None),
        # night-vision
        (["night-vision", "@cams", "auto"], ("setDayNightMode", ("auto",))),
        (["night-vision", "@cams", "on"], ("setDayNightMode", ("on",))),
        (["night-vision", "@cams", "off"], ("setDayNightMode", ("off",))),
        (["night-vision", "@cams", "status"], None),
        # motion (flat positional form)
        (["motion", "@cams", "enable"], ("setMotionDetection", (True,))),
        (["motion", "@cams", "disable"], ("setMotionDetection", (False,))),
        (["motion", "@cams", "status"], None),
        # motion history (sub-verb form)
        (["motion", "history", "@cams", "--limit", "10"], None),
        # alarm
        (["alarm", "@cams", "enable"], ("setAlarm", (True,))),
        (["alarm", "@cams", "disable"], ("setAlarm", (False,))),
        (["alarm", "@cams", "status"], None),
        # audio
        (["audio", "@cams", "volume", "30"], ("setSpeakerVolume", (30,))),
        # osd
        (["osd", "@cams", "status"], None),
        # set
        (["set", "@cams", "--image-flip", "on"], ("setImageFlipVertical", (True,))),
        (["set", "@cams", "--timezone", "UTC"], ("setTimezone", ("UTC", "UTC"))),
        # reboot (with --yes to bypass group prompt)
        (["reboot", "@cams", "--yes"], ("reboot", (None,))),
    ],
)
def test_verb_at_group_fans_out_three_cameras(
    tmp_path: Path, monkeypatch, argv: list[str], expected_call: tuple | None
) -> None:
    """Every FR-43d verb against ``@cams`` (3 members) emits 3 JSONL lines in
    resolved-alias-list order (B9), and (for mutation verbs) calls each
    camera's pytapo surface."""
    tapos = _patch_universal_connect(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_multi_cam_cfg(tmp_path)), "--jsonl", *argv],
    )
    assert result.exit_code == 0, (
        f"argv={argv!r} exited {result.exit_code}\n{result.output}"
    )
    lines = _parse_lines(result.output)
    assert len(lines) == 3, (
        f"argv={argv!r} expected 3 JSONL lines, got {len(lines)}: {lines}"
    )
    # B9: resolved-alias-list order — alpha, beta, gamma.
    assert [r["target"] for r in lines] == ["alpha", "beta", "gamma"]
    for r in lines:
        assert r["status"] == "ok", f"{argv!r} got non-ok line: {r}"
    if expected_call is not None:
        # Each camera's fake received the call exactly once.
        for alias in ("alpha", "beta", "gamma"):
            tapo = tapos[alias]
            assert expected_call in tapo.calls, (
                f"{argv!r}: alias={alias} missing call {expected_call!r}; "
                f"calls={tapo.calls!r}"
            )


# ---------------------------------------------------------------------------
# Preset is PTZ-gated (C225 model) — separate test
# ---------------------------------------------------------------------------


def test_preset_list_at_group_fans_out_on_c225(
    tmp_path: Path, monkeypatch
) -> None:
    """Preset is gated by ``preset`` capability; C200 lacks it (exit-5)
    while C225 has it. With a C225 group, fan-out works normally."""
    _patch_universal_connect(monkeypatch, model="C225")
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_multi_cam_cfg(tmp_path, model="C225")),
            "--jsonl",
            "preset", "@cams", "list",
        ],
    )
    assert result.exit_code == 0, result.output
    lines = _parse_lines(result.output)
    assert len(lines) == 3
    assert [r["target"] for r in lines] == ["alpha", "beta", "gamma"]
    for r in lines:
        assert r["status"] == "ok"
        # Preset list returns the {target, action: "list", presets: [...]} shape.
        assert r["result"]["action"] == "list"
        assert isinstance(r["result"]["presets"], list)


# ---------------------------------------------------------------------------
# Carve-outs (FR-43c, plus snapshot --output -)
# ---------------------------------------------------------------------------


def test_stream_at_group_exits_64(tmp_path: Path) -> None:
    """FR-43c carve-out preserved: ``stream @group`` exits 64."""
    cam_creds = tmp_path / "cam.json"
    cam_creds.write_text(
        '{"version": 2, "username": "u", "password": "p"}\n', encoding="utf-8"
    )
    cam_creds.chmod(0o600)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[devices.alpha]\nip = "10.0.0.1"\ncamera_account_file = "{cam_creds}"\n'
        '\n[groups]\ncams = ["alpha"]\n',
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(
        main, ["--jsonl", "--config", str(cfg), "stream", "@cams"]
    )
    assert result.exit_code == 64, result.output


def test_record_at_group_exits_64(tmp_path: Path) -> None:
    """FR-43c carve-out preserved: ``record @group`` exits 64."""
    cam_creds = tmp_path / "cam.json"
    cam_creds.write_text(
        '{"version": 2, "username": "u", "password": "p"}\n', encoding="utf-8"
    )
    cam_creds.chmod(0o600)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[devices.alpha]\nip = "10.0.0.1"\ncamera_account_file = "{cam_creds}"\n'
        '\n[groups]\ncams = ["alpha"]\n',
        encoding="utf-8",
    )
    out = tmp_path / "out.mp4"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--jsonl", "--config", str(cfg),
            "record", "@cams", "--output", str(out), "--duration", "1",
        ],
    )
    assert result.exit_code == 64, result.output


def test_snapshot_at_group_with_stdout_dash_exits_64(tmp_path: Path) -> None:
    """``snapshot @group --output -`` is binary-on-stdout x N -- exit 64.

    Note: the existing FR-11d check (``--output - cannot be combined with
    --json/--jsonl``) fires first under CliRunner's auto-JSONL mode, but
    the exit code is still 64 either way. We pin exit-64 (the stable
    contract); the operator-visible error text is implementation detail.
    """
    cfg = _multi_cam_cfg(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(cfg),
            "snapshot", "@cams", "--output", "-",
        ],
    )
    assert result.exit_code == 64, result.output


def test_snapshot_at_group_without_target_placeholder_exits_64(
    tmp_path: Path,
) -> None:
    """``snapshot @group --output /tmp/snap.jpg`` (no {target}) → exit 64."""
    cfg = _multi_cam_cfg(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(cfg),
            "snapshot", "@cams", "--output", str(tmp_path / "snap.jpg"),
        ],
    )
    assert result.exit_code == 64, result.output
    assert "{target}" in result.output


# ---------------------------------------------------------------------------
# Mixed pass/fail → exit 7 (FR-43a)
# ---------------------------------------------------------------------------


def test_partial_failure_returns_exit_7(tmp_path: Path, monkeypatch) -> None:
    """One camera fails (we make ``beta``'s setLEDEnabled raise); other two
    pass. Exit code SHALL be 7 (partial-failure)."""
    tapos = _patch_universal_connect(monkeypatch)

    def _fail(*args, **kwargs):
        raise RuntimeError("simulated device failure")

    # Pre-populate all three so they exist before --jsonl prints any line.
    for alias in ("alpha", "beta", "gamma"):
        tapos[alias] = _UniversalFakeTapo()
    # beta fails on the LED call.
    tapos["beta"].setLEDEnabled = _fail  # type: ignore[assignment]

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_multi_cam_cfg(tmp_path)), "--jsonl", "led", "@cams", "on"],
    )
    assert result.exit_code == 7, result.output
    # CliRunner mixes stderr into stdout; filter to JSONL B10 envelope lines
    # (every B10 line carries ``target`` AND ``status``).
    raw_lines = _parse_lines(result.output)
    envelope_lines = [
        r for r in raw_lines if "target" in r and "status" in r
    ]
    assert len(envelope_lines) == 3
    statuses = [r["status"] for r in envelope_lines]
    assert statuses.count("ok") == 2
    assert statuses.count("error") == 1
    # B9 ordering: resolved alias-list order — alpha, beta, gamma.
    assert envelope_lines[1]["target"] == "beta"
    assert envelope_lines[1]["status"] == "error"
