"""Tests for ``tapo-cli set <target> ...`` (Phase 4a, FR-39 / FR-39a / FR-39c)."""

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


def _cfg(tmp_path: Path, *, with_group: bool = False) -> Path:
    cfg_path = tmp_path / "config.toml"
    body = (
        '[devices.office]\n'
        'ip = "192.168.1.11"\n'
        'mac = "AA:BB:CC:DD:EE:02"\n'
        'model = "C200"\n'
    )
    if with_group:
        body += (
            '\n[devices.cam2]\n'
            'ip = "192.168.1.12"\n'
            'mac = "AA:BB:CC:DD:EE:03"\n'
            'model = "C200"\n'
            '\n[groups]\n'
            'all = ["office", "cam2"]\n'
        )
    cfg_path.write_text(body, encoding="utf-8")
    return cfg_path


class _FakeTapo:
    """Mock pytapo.Tapo carrying the set-verb surface."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.flip_state: bool = False
        self.timezone: tuple[str, str] | None = None
        self.basic_info = {"device_info": {"basic_info": {"device_model": "C200"}}}

    def setImageFlipVertical(self, enable: bool, chn_id=None) -> None:  # noqa: N802
        self.flip_state = enable
        self.calls.append(("setImageFlipVertical", (enable,)))

    def setTimezone(self, timezone: str, zoneID: str, timingMode: str = "ntp") -> None:  # noqa: N802, N803
        self.timezone = (timezone, zoneID)
        self.calls.append(("setTimezone", (timezone, zoneID, timingMode)))

    def getBasicInfo(self) -> dict[str, Any]:  # noqa: N802
        return self.basic_info


def _patch_connect_per_alias(monkeypatch, tapos: dict[str, _FakeTapo]) -> None:
    """Patch wrapper.connect to return a different fake per resolved alias."""

    async def _fake_connect(cfg, target, *, credential_source=None, timeout=5.0):
        alias = target
        tapo = tapos.get(alias) or next(iter(tapos.values()))
        return TapoConnection(
            tapo=tapo,
            target=TapoTarget(alias=alias, ip=f"192.168.1.{20 + len(alias)}"),
            credential_family="camera_account",
        )

    monkeypatch.setattr("tapo_cli.wrapper.connect", _fake_connect)


# ---------------------------------------------------------------------------
# --image-flip
# ---------------------------------------------------------------------------


def test_set_image_flip_on_calls_pytapo(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    _patch_connect_per_alias(monkeypatch, {"office": fake})
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--json", "set", "office", "--image-flip", "on"],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed["target"] == "office"
    assert parsed["changes"] == {"image_flip": True}
    assert ("setImageFlipVertical", (True,)) in fake.calls


def test_set_image_flip_off_calls_pytapo(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    fake.flip_state = True  # start flipped
    _patch_connect_per_alias(monkeypatch, {"office": fake})
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--json", "set", "office", "--image-flip", "off"],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed["changes"]["image_flip"] is False
    assert fake.flip_state is False


# ---------------------------------------------------------------------------
# --timezone
# ---------------------------------------------------------------------------


def test_set_timezone_calls_pytapo_with_iana_in_both_slots(
    tmp_path: Path, monkeypatch
) -> None:
    """pytapo.setTimezone(timezone, zoneID); we pass the IANA value as both."""
    fake = _FakeTapo()
    _patch_connect_per_alias(monkeypatch, {"office": fake})
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--json",
            "set", "office", "--timezone", "America/Vancouver",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed["changes"] == {"timezone": "America/Vancouver"}
    # Both pytapo args got the IANA name.
    assert fake.timezone == ("America/Vancouver", "America/Vancouver")


def test_set_combined_image_flip_and_timezone(
    tmp_path: Path, monkeypatch
) -> None:
    """Both flags in one invocation → both pytapo calls fire and changes JSON
    includes both fields."""
    fake = _FakeTapo()
    _patch_connect_per_alias(monkeypatch, {"office": fake})
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--json",
            "set", "office",
            "--image-flip", "on",
            "--timezone", "America/Toronto",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed["changes"] == {
        "image_flip": True,
        "timezone": "America/Toronto",
    }
    # Order: image-flip first, then timezone (matches set_cmd._execute_set).
    fn_names = [c[0] for c in fake.calls]
    assert fn_names == ["setImageFlipVertical", "setTimezone"]


# ---------------------------------------------------------------------------
# Missing-flag exit-64
# ---------------------------------------------------------------------------


def test_set_with_no_flags_exits_64(tmp_path: Path, monkeypatch) -> None:
    """Bare ``set <target>`` without any flag is a usage error (exit 64)."""
    fake = _FakeTapo()
    _patch_connect_per_alias(monkeypatch, {"office": fake})
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--jsonl", "set", "office"],
    )
    assert result.exit_code == 64, result.output
    err = json.loads(result.output.strip().splitlines()[-1])
    assert err["error"] == "usage_error"
    assert "image-flip" in err["message"] or "timezone" in err["message"]
    # No pytapo calls were made.
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Group fan-out (FR-43d)
# ---------------------------------------------------------------------------


def test_set_group_fans_out_two_cameras(tmp_path: Path, monkeypatch) -> None:
    """``set @all --image-flip on`` against a 2-member group emits 2 JSONL
    lines in resolved-alias-list order (B9)."""
    fake_office = _FakeTapo()
    fake_cam2 = _FakeTapo()
    _patch_connect_per_alias(
        monkeypatch, {"office": fake_office, "cam2": fake_cam2}
    )
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path, with_group=True)),
            "--jsonl",
            "set", "@all",
            "--image-flip", "on",
        ],
    )
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    # B9 deterministic: resolved-alias-list order, NOT completion order.
    assert [r["target"] for r in parsed] == ["office", "cam2"]
    for r in parsed:
        assert r["status"] == "ok"
        assert r["result"]["changes"] == {"image_flip": True}
    # Both fakes received the call.
    assert ("setImageFlipVertical", (True,)) in fake_office.calls
    assert ("setImageFlipVertical", (True,)) in fake_cam2.calls
