"""Tests for ``tapo-cli night-vision`` (FR-32, Phase 1d)."""

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
    def __init__(self, *, initial: str = "auto") -> None:
        self._mode = initial
        self.calls: list[Any] = []

    def setDayNightMode(self, mode: str) -> None:  # noqa: N802
        # pytapo strictly validates the set {"off", "on", "auto"}.
        if mode not in {"off", "on", "auto"}:
            raise ValueError(f"unsupported day-night mode: {mode!r}")
        self.calls.append(("set", mode))
        self._mode = mode

    def getDayNightMode(self) -> str:  # noqa: N802
        return self._mode


def _patch_connect(monkeypatch, tapo: _FakeTapo) -> None:
    async def _fake_connect(cfg, target, *, credential_source=None, timeout=5.0):
        return TapoConnection(
            tapo=tapo,
            target=TapoTarget(alias="office", ip="192.168.1.11"),
            credential_family="camera_account",
        )

    monkeypatch.setattr("tapo_cli.wrapper.connect", _fake_connect)


def test_night_vision_auto(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo(initial="off")
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--json", "night-vision", "office", "auto"],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed == {"target": "office", "night_vision_mode": "auto"}
    assert ("set", "auto") in fake.calls


def test_night_vision_on(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo(initial="auto")
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--json", "night-vision", "office", "on"],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["night_vision_mode"] == "on"
    assert ("set", "on") in fake.calls


def test_night_vision_off(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo(initial="auto")
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--json", "night-vision", "office", "off"],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["night_vision_mode"] == "off"
    assert ("set", "off") in fake.calls


def test_night_vision_ir_only_maps_to_on(tmp_path: Path, monkeypatch) -> None:
    """``ir-only`` is a CLI carve-out that maps to pytapo's ``on``."""
    fake = _FakeTapo(initial="auto")
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--json", "night-vision", "office", "ir-only"],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    # Output preserves the requested sub-verb (operator's intent).
    assert parsed["night_vision_mode"] == "ir-only"
    # But the wire payload sent to pytapo is "on" (only legal value).
    assert ("set", "on") in fake.calls
    # And NOT "ir-only" — pytapo would have rejected that.
    assert all(c[1] != "ir-only" for c in fake.calls if c[0] == "set")


def test_night_vision_status_reads_device_value(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo(initial="auto")
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--json", "night-vision", "office", "status"],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["night_vision_mode"] == "auto"
    assert all(c[0] != "set" for c in fake.calls)
