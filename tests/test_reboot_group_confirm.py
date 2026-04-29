"""Tests for ``tapo-cli reboot @group`` confirmation (Phase 4a, FR-43e / FR-43f).

Group reboot applies FR-38's confirmation rules at the GROUP level — one
prompt naming the resolved member list on stderr, NOT one prompt per
camera. ``--yes`` and ``--quiet`` short-circuit; non-tty without ``--yes``
exits 64.
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


def _cfg(tmp_path: Path) -> Path:
    """Write a config with a 2-member group ``perimeter``."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[devices.front]\n'
        'ip = "192.168.1.11"\nmac = "AA:BB:CC:DD:EE:01"\nmodel = "C200"\n'
        '\n[devices.back]\n'
        'ip = "192.168.1.12"\nmac = "AA:BB:CC:DD:EE:02"\nmodel = "C200"\n'
        '\n[groups]\n'
        'perimeter = ["front", "back"]\n',
        encoding="utf-8",
    )
    return cfg_path


class _FakeTapo:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    def reboot(self, delay: object = None) -> dict[str, int]:
        self.calls.append(("reboot", delay))
        return {"error_code": 0}


def _patch_connect_per_alias(
    monkeypatch, tapos: dict[str, _FakeTapo]
) -> None:
    async def _fake_connect(cfg, target, *, credential_source=None, timeout=5.0):
        tapo = tapos.get(target) or next(iter(tapos.values()))
        return TapoConnection(
            tapo=tapo,
            target=TapoTarget(alias=target, ip=f"192.168.1.{20 + len(target)}"),
            credential_family="camera_account",
        )

    monkeypatch.setattr("tapo_cli.wrapper.connect", _fake_connect)


# ---------------------------------------------------------------------------
# Non-tty without --yes → exit 64
# ---------------------------------------------------------------------------


def test_reboot_group_non_tty_without_yes_exits_64(
    tmp_path: Path, monkeypatch
) -> None:
    """CliRunner is non-tty by default → group reboot needs --yes."""
    fake_front = _FakeTapo()
    fake_back = _FakeTapo()
    _patch_connect_per_alias(
        monkeypatch, {"front": fake_front, "back": fake_back}
    )
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--jsonl", "reboot", "@perimeter"],
    )
    assert result.exit_code == 64, result.output
    err = json.loads(result.output.strip().splitlines()[-1])
    assert err["error"] == "usage_error"
    assert "--yes" in err.get("hint", "")
    # Critically: NO reboot calls were made on either camera.
    assert fake_front.calls == []
    assert fake_back.calls == []


# ---------------------------------------------------------------------------
# --yes proceeds, fans out, no per-camera prompts
# ---------------------------------------------------------------------------


def test_reboot_group_with_yes_fans_out(tmp_path: Path, monkeypatch) -> None:
    """``reboot @group --yes`` fans out and emits one JSONL line per camera
    in resolved-alias-list order (B9). Both cameras get a reboot call."""
    fake_front = _FakeTapo()
    fake_back = _FakeTapo()
    _patch_connect_per_alias(
        monkeypatch, {"front": fake_front, "back": fake_back}
    )
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--jsonl",
            "reboot", "@perimeter", "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    # B9: resolved-alias-list order.
    assert [r["target"] for r in parsed] == ["front", "back"]
    for r in parsed:
        assert r["status"] == "ok"
        assert r["result"]["status"] == "reboot-issued"
    # Both fakes received exactly one reboot call.
    assert len([c for c in fake_front.calls if c[0] == "reboot"]) == 1
    assert len([c for c in fake_back.calls if c[0] == "reboot"]) == 1


# ---------------------------------------------------------------------------
# --quiet implies --yes (FR-38 / FR-43e)
# ---------------------------------------------------------------------------


def test_reboot_group_quiet_implies_yes(tmp_path: Path, monkeypatch) -> None:
    """``--quiet`` short-circuits the group prompt and proceeds to fan-out.

    Note: --quiet suppresses stdout; we assert on the device-side calls.
    """
    fake_front = _FakeTapo()
    fake_back = _FakeTapo()
    _patch_connect_per_alias(
        monkeypatch, {"front": fake_front, "back": fake_back}
    )
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--quiet",
            "reboot", "@perimeter",
        ],
    )
    assert result.exit_code == 0, result.output
    # --quiet means stdout is suppressed; no JSONL emitted.
    assert result.output == ""
    # But both cameras got rebooted.
    assert any(c[0] == "reboot" for c in fake_front.calls)
    assert any(c[0] == "reboot" for c in fake_back.calls)


# ---------------------------------------------------------------------------
# Single-target reboot still honors FR-38 (regression safety)
# ---------------------------------------------------------------------------


def test_reboot_single_target_with_yes_still_works(
    tmp_path: Path, monkeypatch
) -> None:
    """FR-38 single-target path is unchanged — Phase 4a only adds group path."""
    fake = _FakeTapo()
    _patch_connect_per_alias(monkeypatch, {"front": fake})
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--json", "reboot", "front", "--yes"],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed == {"target": "front", "status": "reboot-issued"}
    assert any(c[0] == "reboot" for c in fake.calls)
