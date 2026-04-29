"""Tests for ``tapo-cli alarm <target> ...`` (FR-22..24, FR-26..29)."""

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


def _cfg(tmp_path: Path, *, model: str = "C320WS") -> Path:
    """Default to C320WS — the v1 manual-trigger-capable model."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f'[devices.office]\nip = "192.168.1.11"\nmac = "AA:BB:CC:DD:EE:02"\n'
        f'model = "{model}"\n',
        encoding="utf-8",
    )
    return cfg_path


class _FakeTapo:
    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled
        self.calls: list[Any] = []

    def setAlarm(  # noqa: N802
        self,
        enabled: bool,
        soundEnabled: bool = True,  # noqa: N803
        lightEnabled: bool = True,  # noqa: N803
        alarmVolume: int | None = None,  # noqa: N803
        alarmDuration: int | None = None,  # noqa: N803
        alarmType: str | None = None,  # noqa: N803
    ) -> None:
        self.calls.append(("setAlarm", enabled, soundEnabled, lightEnabled))
        self.enabled = enabled

    def getAlarm(self) -> dict[str, Any]:  # noqa: N802
        return {
            "enabled": "on" if self.enabled else "off",
            "alarm_mode": ["sound", "light"],
        }

    def startManualAlarm(self) -> None:  # noqa: N802
        self.calls.append(("startManualAlarm",))

    def stopManualAlarm(self) -> None:  # noqa: N802
        self.calls.append(("stopManualAlarm",))


def _patch_connect(monkeypatch, tapo: _FakeTapo, model: str = "C320WS") -> None:
    async def _fake_connect(cfg, target, *, credential_source=None, timeout=5.0):
        return TapoConnection(
            tapo=tapo,
            target=TapoTarget(alias="office", ip="192.168.1.11", model=model),
            credential_family="camera_account",
        )

    monkeypatch.setattr("tapo_cli.wrapper.connect", _fake_connect)


# ---------------------------------------------------------------------------
# enable / disable
# ---------------------------------------------------------------------------


def test_alarm_enable_calls_setalarm_true(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo(enabled=False)
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--json", "alarm", "office", "enable"],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["action"] == "enable"
    assert parsed["alarm_enabled"] is True
    assert any(c[0] == "setAlarm" and c[1] is True for c in fake.calls)


def test_alarm_disable_calls_setalarm_false(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo(enabled=True)
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--json", "alarm", "office", "disable"],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["action"] == "disable"
    assert parsed["alarm_enabled"] is False
    assert any(c[0] == "setAlarm" and c[1] is False for c in fake.calls)


# ---------------------------------------------------------------------------
# trigger
# ---------------------------------------------------------------------------


def test_alarm_trigger_on_capable_model_calls_startmanualalarm(
    tmp_path: Path, monkeypatch
) -> None:
    """C320WS has alarm_trigger=true → startManualAlarm fires."""
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--json", "alarm", "office", "trigger"],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["action"] == "trigger"
    assert parsed["manual"] is True
    assert ("startManualAlarm",) in fake.calls


def test_alarm_trigger_on_c200_exits_5(tmp_path: Path, monkeypatch) -> None:
    """C200 has alarm=true but alarm_trigger=false → exit 5."""
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake, model="C200")
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path, model="C200")),
            "--json",
            "alarm", "office", "trigger",
        ],
    )
    assert result.exit_code == 5, result.output
    err = json.loads(result.output.strip().splitlines()[-1])
    assert err["error"] == "unsupported_feature"
    # No manual-alarm call was made.
    assert ("startManualAlarm",) not in fake.calls


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_alarm_status_reads_getalarm(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo(enabled=True)
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--json", "alarm", "office", "status"],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["alarm_enabled"] is True
    assert parsed["sound_enabled"] is True
    assert parsed["light_enabled"] is True


def test_alarm_status_on_c200_works(tmp_path: Path, monkeypatch) -> None:
    """C200 has alarm capability but no alarm_trigger; status MUST work."""
    fake = _FakeTapo(enabled=False)
    _patch_connect(monkeypatch, fake, model="C200")
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path, model="C200")),
            "--json",
            "alarm", "office", "status",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["alarm_enabled"] is False
