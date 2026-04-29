"""Tests for ``tapo-cli ptz <target> ...`` (FR-14..17, B7)."""

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


def _cfg(tmp_path: Path, *, model: str = "C200") -> Path:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f'[devices.office]\nip = "192.168.1.11"\nmac = "AA:BB:CC:DD:EE:02"\n'
        f'model = "{model}"\n',
        encoding="utf-8",
    )
    return cfg_path


class _FakeTapo:
    """Mock pytapo.Tapo carrying just the ptz surface."""

    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.basic_info = {"device_info": {"basic_info": {"device_model": "C200"}}}

    def moveMotor(self, x: int, y: int) -> None:  # noqa: N802
        self.calls.append(("moveMotor", x, y))

    def setMotorOff(self) -> None:  # noqa: N802
        self.calls.append(("setMotorOff",))

    def getBasicInfo(self) -> dict[str, Any]:  # noqa: N802
        return self.basic_info


def _patch_connect(monkeypatch, tapo: _FakeTapo, model: str = "C200") -> None:
    async def _fake_connect(cfg, target, *, credential_source=None, timeout=5.0):
        return TapoConnection(
            tapo=tapo,
            target=TapoTarget(alias="office", ip="192.168.1.11", model=model),
            credential_family="camera_account",
        )

    monkeypatch.setattr("tapo_cli.wrapper.connect", _fake_connect)


# ---------------------------------------------------------------------------
# pan / tilt — directional sub-verbs
# ---------------------------------------------------------------------------


def test_ptz_pan_left_sends_negative_x(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--json",
            "ptz", "office", "pan", "left", "--step", "15",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["target"] == "office"
    assert parsed["action"] == "pan"
    assert parsed["direction"] == "left"
    assert parsed["step"] == 15
    # C200 is step-mode → device-step-units.
    assert parsed["step_unit"] == "device-step-units"
    assert ("moveMotor", -15, 0) in fake.calls


def test_ptz_pan_right_sends_positive_x(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--json",
            "ptz", "office", "pan", "right", "--step", "5",
        ],
    )
    assert result.exit_code == 0, result.output
    assert ("moveMotor", 5, 0) in fake.calls


def test_ptz_tilt_up_sends_positive_y(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--json",
            "ptz", "office", "tilt", "up", "--step", "8",
        ],
    )
    assert result.exit_code == 0, result.output
    assert ("moveMotor", 0, 8) in fake.calls


def test_ptz_tilt_down_sends_negative_y(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--json",
            "ptz", "office", "tilt", "down",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["step"] == 10  # default
    assert ("moveMotor", 0, -10) in fake.calls


# ---------------------------------------------------------------------------
# move — combined offset
# ---------------------------------------------------------------------------


def test_ptz_move_combines_pan_and_tilt(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--json",
            "ptz", "office", "move", "--pan", "12", "--tilt", "-4",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["pan"] == 12
    assert parsed["tilt"] == -4
    assert parsed["zoom"] == 0
    assert ("moveMotor", 12, -4) in fake.calls


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


def test_ptz_stop_calls_setmotoroff(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--json", "ptz", "office", "stop"],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["action"] == "stop"
    assert parsed["stopped"] is True
    assert ("setMotorOff",) in fake.calls


# ---------------------------------------------------------------------------
# Unit semantics (B7)
# ---------------------------------------------------------------------------


def test_ptz_pan_step_unit_degrees_on_continuous_model(tmp_path: Path, monkeypatch) -> None:
    """C225 has ptz_mode=continuous → JSON reports step_unit=degrees."""
    fake = _FakeTapo()
    fake.basic_info = {"device_info": {"basic_info": {"device_model": "C225"}}}
    _patch_connect(monkeypatch, fake, model="C225")
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path, model="C225")),
            "--json",
            "ptz", "office", "pan", "right", "--step", "20",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["step_unit"] == "degrees"


def test_ptz_pan_step_unit_device_units_on_step_model(tmp_path: Path, monkeypatch) -> None:
    """C200 is step-mode → device-step-units."""
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--json",
            "ptz", "office", "pan", "right",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["step_unit"] == "device-step-units"


# ---------------------------------------------------------------------------
# Capability gate — non-PTZ models exit 5
# ---------------------------------------------------------------------------


def test_ptz_on_non_ptz_model_exits_5(tmp_path: Path, monkeypatch) -> None:
    """C100 has no PTZ motors → exit 5 with a structured hint."""
    fake = _FakeTapo()
    fake.basic_info = {"device_info": {"basic_info": {"device_model": "C100"}}}
    _patch_connect(monkeypatch, fake, model="C100")
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path, model="C100")),
            "--json",
            "ptz", "office", "pan", "left",
        ],
    )
    assert result.exit_code == 5, result.output
    err = json.loads(result.output.strip().splitlines()[-1])
    assert err["error"] == "unsupported_feature"
    assert err["exit_code"] == 5
    # No moveMotor calls were issued.
    assert all(c[0] != "moveMotor" for c in fake.calls)


def test_ptz_zoom_on_c200_exits_5(tmp_path: Path, monkeypatch) -> None:
    """C200 has step PTZ but no zoom motor → zoom verb exits 5."""
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--json",
            "ptz", "office", "zoom", "in",
        ],
    )
    assert result.exit_code == 5, result.output
    err = json.loads(result.output.strip().splitlines()[-1])
    assert err["error"] == "unsupported_feature"
    # Hint should reference C225 (the zoom-capable model).
    assert "C225" in err.get("hint", "")
