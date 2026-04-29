"""Tests for ``tapo-cli osd <target> ...`` (FR-37, S14)."""

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
        self.calls: list[Any] = []
        self.osd_state = {
            "label": "",
            "labelEnabled": False,
            "dateEnabled": True,
        }
        self.basic_info = {"device_info": {"basic_info": {"device_model": "C200"}}}

    def setOsd(  # noqa: N802
        self,
        label: str,
        dateEnabled: bool = True,  # noqa: N803
        labelEnabled: bool = False,  # noqa: N803
        weekEnabled: bool = False,  # noqa: N803
        logoEnabled: bool = False,  # noqa: N803
        dateX: int = 0,  # noqa: N803
        dateY: int = 0,  # noqa: N803
        labelX: int = 0,  # noqa: N803
        labelY: int = 500,  # noqa: N803
        weekX: int = 0,  # noqa: N803
        weekY: int = 0,  # noqa: N803
        logoX: int = 0,  # noqa: N803
        logoY: int = 0,  # noqa: N803
    ) -> None:
        self.calls.append(("setOsd", label, dateEnabled, labelEnabled, labelX, labelY))
        self.osd_state["label"] = label
        self.osd_state["labelEnabled"] = labelEnabled
        self.osd_state["dateEnabled"] = dateEnabled

    def getOsd(self) -> dict[str, Any]:  # noqa: N802
        return dict(self.osd_state)

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
# status — works on every model (osd_timestamp=true everywhere)
# ---------------------------------------------------------------------------


def test_osd_status_on_c200_returns_timestamp_on(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    fake.osd_state["dateEnabled"] = True
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--json", "osd", "office", "status"],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["action"] == "status"
    assert parsed["timestamp_on"] is True


def test_osd_status_reports_label_when_enabled(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    fake.osd_state["labelEnabled"] = True
    fake.osd_state["label"] = "FRONT DOOR"
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--json", "osd", "office", "status"],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["label"] == "FRONT DOOR"
    assert parsed["label_on"] is True


# ---------------------------------------------------------------------------
# set --text — capability-gate on C200 (osd_text=false)
# ---------------------------------------------------------------------------


def test_osd_set_text_on_c200_exits_5(tmp_path: Path, monkeypatch) -> None:
    """C200 has osd_text=false → exit 5 BEFORE codepoint check."""
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--json",
            "osd", "office", "set", "--text", "hi",
        ],
    )
    assert result.exit_code == 5, result.output
    err = json.loads(result.output.strip().splitlines()[-1])
    assert err["error"] == "unsupported_feature"
    # Hint references a supporting model (C225 has osd_text=true).
    assert "C225" in err.get("hint", "") or "C320WS" in err.get("hint", "")
    # No setOsd was called.
    assert all(c[0] != "setOsd" for c in fake.calls)


def test_osd_set_text_on_c225_succeeds(tmp_path: Path, monkeypatch) -> None:
    """C225 has osd_text=true → setOsd fires."""
    fake = _FakeTapo()
    fake.basic_info = {"device_info": {"basic_info": {"device_model": "C225"}}}
    _patch_connect(monkeypatch, fake, model="C225")
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path, model="C225")),
            "--json",
            "osd", "office", "set", "--text", "FRONT-DOOR",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["text"] == "FRONT-DOOR"
    assert parsed["position"] == "bl"  # default
    assert any(c[0] == "setOsd" and c[1] == "FRONT-DOOR" for c in fake.calls)


# ---------------------------------------------------------------------------
# Codepoint counting (FR-37a, S14)
# ---------------------------------------------------------------------------


def test_osd_set_text_exactly_32_codepoints_succeeds(tmp_path: Path, monkeypatch) -> None:
    """The cap is 32 codepoints — exactly 32 is allowed."""
    fake = _FakeTapo()
    fake.basic_info = {"device_info": {"basic_info": {"device_model": "C225"}}}
    _patch_connect(monkeypatch, fake, model="C225")
    runner = CliRunner()
    text = "x" * 32
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path, model="C225")),
            "--json",
            "osd", "office", "set", "--text", text,
        ],
    )
    assert result.exit_code == 0, result.output


def test_osd_set_text_33_codepoints_exits_64(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    fake.basic_info = {"device_info": {"basic_info": {"device_model": "C225"}}}
    _patch_connect(monkeypatch, fake, model="C225")
    runner = CliRunner()
    text = "x" * 33
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path, model="C225")),
            "--json",
            "osd", "office", "set", "--text", text,
        ],
    )
    assert result.exit_code == 64, result.output
    err = json.loads(result.output.strip().splitlines()[-1])
    assert err["error"] == "usage_error"


def test_osd_set_text_multibyte_glyph_counts_as_one(tmp_path: Path, monkeypatch) -> None:
    """A 4-byte CJK character counts as 1 codepoint, not 4. 30 such chars OK."""
    fake = _FakeTapo()
    fake.basic_info = {"device_info": {"basic_info": {"device_model": "C225"}}}
    _patch_connect(monkeypatch, fake, model="C225")
    runner = CliRunner()
    # 30 CJK glyphs (each 1 codepoint, ~3 UTF-8 bytes). 30 < 32 → OK.
    text = "中" * 30
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path, model="C225")),
            "--json",
            "osd", "office", "set", "--text", text,
        ],
    )
    assert result.exit_code == 0, result.output


def test_osd_set_text_emoji_counts_as_codepoints(tmp_path: Path, monkeypatch) -> None:
    """A simple emoji is one codepoint per Python's ``len()``. 32 fits, 33 doesn't."""
    fake = _FakeTapo()
    fake.basic_info = {"device_info": {"basic_info": {"device_model": "C225"}}}
    _patch_connect(monkeypatch, fake, model="C225")
    runner = CliRunner()
    text = "★" * 33  # 33 codepoints → exit 64
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path, model="C225")),
            "--json",
            "osd", "office", "set", "--text", text,
        ],
    )
    assert result.exit_code == 64, result.output


# ---------------------------------------------------------------------------
# clear — capability-gated on osd_text
# ---------------------------------------------------------------------------


def test_osd_clear_on_c200_exits_5(tmp_path: Path, monkeypatch) -> None:
    """Clearing requires osd_text capability — C200 lacks it."""
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--json", "osd", "office", "clear"],
    )
    assert result.exit_code == 5, result.output


def test_osd_clear_on_c225_succeeds(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    fake.basic_info = {"device_info": {"basic_info": {"device_model": "C225"}}}
    fake.osd_state = {"label": "OLD", "labelEnabled": True, "dateEnabled": True}
    _patch_connect(monkeypatch, fake, model="C225")
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path, model="C225")),
            "--json",
            "osd", "office", "clear",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["action"] == "clear"
    # Last setOsd call should disable the label.
    last_set = [c for c in fake.calls if c[0] == "setOsd"][-1]
    # (call, label, date_enabled, label_enabled, label_x, label_y)
    assert last_set[3] is False  # labelEnabled


# ---------------------------------------------------------------------------
# Timestamp toggle — works on any model with osd_timestamp (all of them)
# ---------------------------------------------------------------------------


def test_osd_set_show_time_toggles_on_c200(tmp_path: Path, monkeypatch) -> None:
    """C200 doesn't support osd_text but DOES support osd_timestamp."""
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--json",
            "osd", "office", "set", "--show-time",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["show_time"] is True


def test_osd_set_no_args_exits_64(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--json", "osd", "office", "set"],
    )
    assert result.exit_code == 64, result.output
