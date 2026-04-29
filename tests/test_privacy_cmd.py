"""Tests for ``tapo-cli privacy <target> enable|disable|status`` (Phase 1d)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from tapo_cli.cli import main
from tapo_cli.errors import AuthError
from tapo_cli.wrapper import TapoConnection, TapoTarget


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TAPO_CLI_CONFIG", raising=False)
    monkeypatch.delenv("TAPO_USERNAME", raising=False)
    monkeypatch.delenv("TAPO_PASSWORD", raising=False)


def _cfg(tmp_path: Path) -> Path:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[devices.office]\nip = "192.168.1.11"\nmac = "AA:BB:CC:DD:EE:02"\nmodel = "C200"\n',
        encoding="utf-8",
    )
    return cfg_path


class _FakeTapo:
    def __init__(self, *, initial: bool = False) -> None:
        self._enabled = initial
        self.calls: list[Any] = []

    # pytapo API names — mixed case retained.
    def setPrivacyMode(self, enabled: bool) -> None:  # noqa: N802
        self.calls.append(("set", enabled))
        self._enabled = enabled

    def getPrivacyMode(self) -> dict[str, str]:  # noqa: N802
        return {"enabled": "on" if self._enabled else "off"}


def _patch_connect(monkeypatch, tapo: _FakeTapo) -> None:
    async def _fake_connect(cfg, target, *, credential_source=None, timeout=5.0):
        return TapoConnection(
            tapo=tapo,
            target=TapoTarget(alias="office", ip="192.168.1.11"),
            credential_family="camera_account",
        )

    monkeypatch.setattr("tapo_cli.wrapper.connect", _fake_connect)


def test_privacy_enable_returns_true(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo(initial=False)
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "--json", "privacy", "office", "enable"]
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed == {"target": "office", "privacy_enabled": True}
    assert ("set", True) in fake.calls


def test_privacy_disable_returns_false(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo(initial=True)
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "--json", "privacy", "office", "disable"]
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed == {"target": "office", "privacy_enabled": False}
    assert ("set", False) in fake.calls


def test_privacy_status_reads_state(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo(initial=True)
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "--json", "privacy", "office", "status"]
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["target"] == "office"
    assert parsed["privacy_enabled"] is True
    # status MUST NOT have called the setter.
    assert all(c[0] != "set" for c in fake.calls)


def test_privacy_auth_failed_exits_2(tmp_path: Path, monkeypatch) -> None:
    async def _always_auth_fail(cfg, target, *, credential_source=None, timeout=5.0):
        raise AuthError(
            "pytapo auth failed for office via camera_account",
            target="office",
            credential="camera_account",
        )

    monkeypatch.setattr("tapo_cli.wrapper.connect", _always_auth_fail)
    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "--json", "privacy", "office", "status"]
    )
    assert result.exit_code == 2, result.output
    err = json.loads(result.output.strip().splitlines()[-1])
    assert err["error"] == "auth_failed"


def test_privacy_invalid_action_exits_64(tmp_path: Path) -> None:
    """Click choice validation rejects ``toggle``."""
    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "privacy", "office", "toggle"]
    )
    assert result.exit_code == 2  # Click's default for choice errors


def test_privacy_strips_at_alias_prefix(tmp_path: Path, monkeypatch) -> None:
    """``@office`` should resolve as the bare alias ``office``."""
    fake = _FakeTapo(initial=False)
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "--json", "privacy", "@office", "enable"]
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["privacy_enabled"] is True
