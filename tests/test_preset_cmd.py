"""Tests for ``tapo-cli preset <target> ...`` (FR-18..21)."""

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
    def __init__(self) -> None:
        # Pre-populate one preset for the goto/delete tests.
        self.presets: dict[str, str] = {"1": "desk", "2": "door"}
        self.calls: list[Any] = []

    def getPresets(self) -> dict[str, str]:  # noqa: N802
        # Return a copy so callers can't mutate our state.
        return dict(self.presets)

    def setPreset(self, presetID: int | str, retry: bool = False) -> None:  # noqa: N802, N803
        self.calls.append(("setPreset", presetID))

    def savePreset(self, name: str) -> None:  # noqa: N802
        self.calls.append(("savePreset", name))
        # Allocate the next id.
        next_id = max((int(k) for k in self.presets), default=0) + 1
        self.presets[str(next_id)] = name

    def deletePreset(self, presetID: int | str, retry: bool = False) -> None:  # noqa: N802, N803
        self.calls.append(("deletePreset", presetID))
        self.presets.pop(str(presetID), None)


def _patch_connect(monkeypatch, tapo: _FakeTapo) -> None:
    async def _fake_connect(cfg, target, *, credential_source=None, timeout=5.0):
        return TapoConnection(
            tapo=tapo,
            target=TapoTarget(alias="office", ip="192.168.1.11", model="C200"),
            credential_family="camera_account",
        )

    monkeypatch.setattr("tapo_cli.wrapper.connect", _fake_connect)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_preset_list_emits_array(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--json", "preset", "office", "list"],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, list)
    names = {p["name"] for p in parsed}
    assert names == {"desk", "door"}


def test_preset_list_sorts_by_id_ascending(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    fake.presets = {"3": "third", "1": "first", "2": "second"}
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--json", "preset", "office", "list"],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert [p["name"] for p in parsed] == ["first", "second", "third"]


# ---------------------------------------------------------------------------
# goto
# ---------------------------------------------------------------------------


def test_preset_goto_resolves_name_to_id(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--json",
            "preset", "office", "goto", "desk",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["action"] == "goto"
    assert parsed["name"] == "desk"
    assert parsed["preset_id"] == 1
    # setPreset called with id 1.
    assert any(c[0] == "setPreset" and c[1] == 1 for c in fake.calls)


def test_preset_goto_case_insensitive_match(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--json",
            "preset", "office", "goto", "DESK",
        ],
    )
    assert result.exit_code == 0, result.output


def test_preset_goto_unknown_name_exits_4(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--json",
            "preset", "office", "goto", "nonexistent",
        ],
    )
    assert result.exit_code == 4, result.output
    err = json.loads(result.output.strip().splitlines()[-1])
    assert err["error"] == "not_found"
    assert err["exit_code"] == 4


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------


def test_preset_save_creates_new_preset(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--json",
            "preset", "office", "save", "view-1",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["action"] == "save"
    assert parsed["name"] == "view-1"
    assert parsed["preset_id"] == 3  # 1=desk, 2=door, 3=view-1
    assert any(c[0] == "savePreset" and c[1] == "view-1" for c in fake.calls)


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_preset_delete_resolves_name_to_id(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--json",
            "preset", "office", "delete", "door",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["action"] == "delete"
    assert parsed["name"] == "door"
    assert parsed["preset_id"] == 2
    assert any(c[0] == "deletePreset" and c[1] == 2 for c in fake.calls)


def test_preset_delete_unknown_name_exits_4(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--json",
            "preset", "office", "delete", "ghost",
        ],
    )
    assert result.exit_code == 4, result.output


# ---------------------------------------------------------------------------
# Capability gate — non-preset models exit 5
# ---------------------------------------------------------------------------


def test_preset_list_on_c100_exits_5(tmp_path: Path, monkeypatch) -> None:
    """C100 has no PTZ motors → preset capability false → exit 5."""

    class _NoPresetTapo:
        def getBasicInfo(self) -> dict[str, Any]:  # noqa: N802
            return {"device_info": {"basic_info": {"device_model": "C100"}}}

        def getPresets(self) -> dict[str, str]:  # noqa: N802
            return {}

    async def _fake_connect(cfg, target, *, credential_source=None, timeout=5.0):
        return TapoConnection(
            tapo=_NoPresetTapo(),
            target=TapoTarget(alias="office", ip="192.168.1.11", model="C100"),
            credential_family="camera_account",
        )

    monkeypatch.setattr("tapo_cli.wrapper.connect", _fake_connect)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path, model="C100")),
            "--json",
            "preset", "office", "list",
        ],
    )
    assert result.exit_code == 5, result.output
