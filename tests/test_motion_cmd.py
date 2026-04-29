"""Tests for ``tapo-cli motion <target> enable|disable|status|history``."""

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


def _cfg(tmp_path: Path) -> Path:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[devices.office]\nip = "192.168.1.11"\nmac = "AA:BB:CC:DD:EE:02"\n',
        encoding="utf-8",
    )
    return cfg_path


class _FakeTapo:
    def __init__(self, *, initial: bool = True, sensitivity: str = "medium") -> None:
        self._enabled = initial
        self._sensitivity = sensitivity
        self.calls: list[Any] = []

    def setMotionDetection(  # noqa: N802 — pytapo API name
        self,
        enabled: bool | None = None,
        sensitivity: bool | str = False,
        chn_id: list[int] | None = None,
    ) -> None:
        self.calls.append(("set", enabled, sensitivity, chn_id))
        if enabled is not None:
            self._enabled = enabled

    def getMotionDetection(self, chn_id: list[int] | None = None) -> dict[str, Any]:  # noqa: N802
        return {
            "enabled": "on" if self._enabled else "off",
            "digital_sensitivity": "60",
            "sensitivity": self._sensitivity,
        }


def _patch_connect(monkeypatch, tapo: _FakeTapo) -> None:
    async def _fake_connect(cfg, target, *, credential_source=None, timeout=5.0):
        return TapoConnection(
            tapo=tapo,
            target=TapoTarget(alias="office", ip="192.168.1.11"),
            credential_family="camera_account",
        )

    monkeypatch.setattr("tapo_cli.wrapper.connect", _fake_connect)


def test_motion_enable(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo(initial=False)
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "--json", "motion", "office", "enable"]
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["target"] == "office"
    assert parsed["motion_enabled"] is True
    # No ``sensitivity`` field on enable/disable — only on status.
    assert "sensitivity" not in parsed
    assert any(c[0] == "set" and c[1] is True for c in fake.calls)


def test_motion_disable(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo(initial=True)
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "--json", "motion", "office", "disable"]
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["motion_enabled"] is False
    assert any(c[0] == "set" and c[1] is False for c in fake.calls)


def test_motion_status_includes_sensitivity(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo(initial=True, sensitivity="high")
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "--json", "motion", "office", "status"]
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["motion_enabled"] is True
    assert parsed["sensitivity"] == "high"
    assert all(c[0] != "set" for c in fake.calls)


def test_motion_history_exits_5(tmp_path: Path, monkeypatch) -> None:
    """history is reserved for Phase 3; Phase 1d returns
    ``unsupported_feature`` (exit 5) with a hint pointing forward."""
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "--json", "motion", "office", "history"]
    )
    assert result.exit_code == 5, result.output
    err = json.loads(result.output.strip().splitlines()[-1])
    assert err["error"] == "unsupported_feature"
    assert err["exit_code"] == 5
    assert "Phase 3" in err.get("hint", "")
