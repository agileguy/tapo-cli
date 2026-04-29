"""Tests for ``tapo-cli led <target> on|off|status`` (Phase 1d)."""

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
    def __init__(self, *, initial: bool = False) -> None:
        self._enabled = initial
        self.calls: list[Any] = []

    def setLEDEnabled(self, enabled: bool) -> None:  # noqa: N802
        self.calls.append(("set", enabled))
        self._enabled = enabled

    def getLED(self) -> dict[str, str]:  # noqa: N802
        return {"enabled": "on" if self._enabled else "off"}


def _patch_connect(monkeypatch, tapo: _FakeTapo) -> None:
    async def _fake_connect(cfg, target, *, credential_source=None, timeout=5.0):
        return TapoConnection(
            tapo=tapo,
            target=TapoTarget(alias="office", ip="192.168.1.11"),
            credential_family="camera_account",
        )

    monkeypatch.setattr("tapo_cli.wrapper.connect", _fake_connect)


def test_led_on_returns_true(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo(initial=False)
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "--json", "led", "office", "on"]
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed == {"target": "office", "led_enabled": True}
    assert ("set", True) in fake.calls


def test_led_off_returns_false(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo(initial=True)
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "--json", "led", "office", "off"]
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed == {"target": "office", "led_enabled": False}
    assert ("set", False) in fake.calls


def test_led_status_reads_state(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo(initial=True)
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "--json", "led", "office", "status"]
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["led_enabled"] is True
    assert all(c[0] != "set" for c in fake.calls)


def test_led_status_text_mode(tmp_path: Path, monkeypatch) -> None:
    """JSONL on non-tty (default for CliRunner) should still emit one JSON line."""
    fake = _FakeTapo(initial=False)
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "led", "office", "status"]
    )
    assert result.exit_code == 0, result.output
    line = result.output.strip().splitlines()[-1]
    parsed = json.loads(line)
    assert parsed["target"] == "office"
    assert parsed["led_enabled"] is False
