"""Tests for ``tapo-cli info`` (FR-9, FR-10, FR-CRED-8/8.1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from tapo_cli import device_info
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
        """
[devices.office]
ip = "192.168.1.11"
mac = "AA:BB:CC:DD:EE:02"
model = "C200"
""".lstrip(),
        encoding="utf-8",
    )
    return cfg_path


class _FakeTapo:
    """Minimal pytapo stand-in returning canned getBasicInfo payloads."""

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def getBasicInfo(self) -> dict[str, object]:  # noqa: N802 — pytapo API name
        return self._payload


def test_info_emits_full_camera_record(tmp_path: Path, monkeypatch) -> None:
    payload = {
        "device_info": {
            "basic_info": {
                "device_model": "C200",
                "fw_version": "1.3.4 Build 240115 Rel.74000n",
                "hw_version": "2.0",
                "mac": "AA-BB-CC-DD-EE-02",
            }
        }
    }

    async def _fake_connect(cfg, target, *, credential_source=None, timeout=5.0):
        return TapoConnection(
            tapo=_FakeTapo(payload),
            target=TapoTarget(alias="office", ip="192.168.1.11", mac="AA:BB:CC:DD:EE:02"),
            credential_family="camera_account",
        )

    monkeypatch.setattr("tapo_cli.wrapper.connect", _fake_connect)

    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "--json", "info", "office"]
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["alias"] == "office"
    assert parsed["ip"] == "192.168.1.11"
    assert parsed["mac"] == "AA:BB:CC:DD:EE:02"
    assert parsed["model"] == "C200"
    assert parsed["firmware_version"].startswith("1.3.4")
    assert parsed["hardware_version"] == "2.0"
    assert parsed["supported"] is True
    assert "ptz" in parsed["features"]  # C200 has step-mode PTZ
    assert parsed["last_seen"].endswith("Z")


def test_info_handles_klap_flat_shape(tmp_path: Path, monkeypatch) -> None:
    """KLAP firmware returns ``device_info`` directly, no ``basic_info`` nest."""
    payload = {
        "device_info": {
            "device_model": "C220",
            "fw_version": "1.4.2",
            "hw_version": "1.0",
            "mac": "AABBCCDDEE03",
        }
    }

    async def _fake_connect(cfg, target, *, credential_source=None, timeout=5.0):
        return TapoConnection(
            tapo=_FakeTapo(payload),
            target=TapoTarget(alias="office", ip="192.168.1.11"),
            credential_family="camera_account",
        )

    monkeypatch.setattr("tapo_cli.wrapper.connect", _fake_connect)

    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "--json", "info", "office"]
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["model"] == "C220"
    assert parsed["mac"] == "AA:BB:CC:DD:EE:03"


def test_info_two_consecutive_auth_failures_exit_2(tmp_path: Path, monkeypatch) -> None:
    """Wrapper raises AuthError after exhausting fallback → exit 2."""

    async def _always_auth_fail(cfg, target, *, credential_source=None, timeout=5.0):
        raise AuthError(
            "pytapo auth failed for office via camera_account",
            target="office",
            credential="camera_account",
        )

    monkeypatch.setattr("tapo_cli.wrapper.connect", _always_auth_fail)

    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "--json", "info", "office"]
    )
    assert result.exit_code == 2, result.output
    err = json.loads(result.output.strip().splitlines()[-1])
    assert err["error"] == "auth_failed"
    assert err["exit_code"] == 2


def test_info_strips_at_alias_prefix(tmp_path: Path, monkeypatch) -> None:
    """``@alias`` syntax (target groups) MUST resolve as a plain alias here."""
    captured: dict[str, str] = {}
    payload = {
        "device_info": {
            "basic_info": {
                "device_model": "C200",
                "fw_version": "1.3.4",
                "hw_version": "2.0",
                "mac": "AA:BB:CC:DD:EE:02",
            }
        }
    }

    async def _fake_connect(cfg, target, *, credential_source=None, timeout=5.0):
        captured["target"] = target
        return TapoConnection(
            tapo=_FakeTapo(payload),
            target=TapoTarget(alias="office", ip="192.168.1.11"),
            credential_family="camera_account",
        )

    monkeypatch.setattr("tapo_cli.wrapper.connect", _fake_connect)

    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "--json", "info", "@office"]
    )
    assert result.exit_code == 0, result.output
    assert captured["target"] == "office"


def test_info_accepts_bare_ip(tmp_path: Path, monkeypatch) -> None:
    """A bare IPv4 not in config should still resolve (FR-39 target syntax)."""
    payload = {
        "device_info": {
            "basic_info": {
                "device_model": "C200",
                "fw_version": "1.3.4",
                "hw_version": "2.0",
                "mac": "AA:BB:CC:DD:EE:99",
            }
        }
    }

    async def _fake_connect(cfg, target, *, credential_source=None, timeout=5.0):
        # Wrapper's resolve_target needs a synthesized DeviceEntry — verify
        # it's been planted into the config.
        assert target in cfg.devices
        return TapoConnection(
            tapo=_FakeTapo(payload),
            target=TapoTarget(alias=target, ip=target),
            credential_family="camera_account",
        )

    monkeypatch.setattr("tapo_cli.wrapper.connect", _fake_connect)

    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "--json", "info", "10.0.0.99"]
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["ip"] == "10.0.0.99"


def test_info_features_match_section_3_3_1(tmp_path: Path, monkeypatch) -> None:
    """C225 carries ptz + zoom + dual-lens per the §3.3.1 capability matrix."""
    payload = {
        "device_info": {
            "basic_info": {
                "device_model": "C225",
                "fw_version": "1.0",
                "hw_version": "1.0",
                "mac": "AA:BB:CC:DD:EE:99",
            }
        }
    }

    async def _fake_connect(cfg, target, *, credential_source=None, timeout=5.0):
        return TapoConnection(
            tapo=_FakeTapo(payload),
            target=TapoTarget(alias="office", ip="192.168.1.11"),
            credential_family="camera_account",
        )

    monkeypatch.setattr("tapo_cli.wrapper.connect", _fake_connect)

    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "--json", "info", "office"]
    )
    assert result.exit_code == 0, result.output
    feats = set(json.loads(result.stdout)["features"])
    assert {"ptz", "zoom", "dual-lens"} <= feats


def test_flatten_basic_info_handles_top_level() -> None:
    """A response that's already flat (no device_info wrapper) flattens to itself."""
    flat = {"device_model": "C200", "fw_version": "1.0", "mac": "AA:BB:CC:DD:EE:01"}
    assert device_info.flatten_basic_info(flat) == flat
