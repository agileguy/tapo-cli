"""Tests for ``tapo-cli audio <target> ...`` (FR-33..36, S4)."""

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
        self.basic_info = {"device_info": {"basic_info": {"device_model": "C200"}}}
        self.audio_config: dict[str, Any] = {
            "speaker": {"volume": 70},
            "microphone": {"mute_status": "off"},
        }

    def setSpeakerVolume(self, volume: int) -> None:  # noqa: N802
        self.calls.append(("setSpeakerVolume", volume))
        if isinstance(self.audio_config["speaker"], dict):
            self.audio_config["speaker"]["volume"] = volume

    def setMicrophone(  # noqa: N802
        self,
        volume: int | None = None,
        mute: bool | None = None,
        noise_cancelling: bool | None = None,
    ) -> None:
        self.calls.append(("setMicrophone", volume, mute, noise_cancelling))
        if mute is not None and isinstance(self.audio_config["microphone"], dict):
            self.audio_config["microphone"]["mute_status"] = "on" if mute else "off"

    def getAudioConfig(self) -> dict[str, Any]:  # noqa: N802
        return dict(self.audio_config)

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
# volume
# ---------------------------------------------------------------------------


def test_audio_volume_sets_speaker_volume(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--json",
            "audio", "office", "volume", "60",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["action"] == "volume"
    assert parsed["volume"] == 60
    assert ("setSpeakerVolume", 60) in fake.calls


def test_audio_volume_out_of_range_high_exits_64(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--json",
            "audio", "office", "volume", "150",
        ],
    )
    assert result.exit_code == 64, result.output
    err = json.loads(result.output.strip().splitlines()[-1])
    assert err["error"] == "usage_error"


def test_audio_volume_out_of_range_low_exits_64(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--json",
            "audio", "office", "volume", "-1",
        ],
    )
    # Click parses ``-1`` as an option, not an int arg, so exit may be 2 from
    # Click's argument parser. We accept Click's 2 OR our own 64. Either way
    # the speaker isn't called.
    assert result.exit_code in (2, 64), result.output
    assert all(c[0] != "setSpeakerVolume" for c in fake.calls)


# ---------------------------------------------------------------------------
# mic
# ---------------------------------------------------------------------------


def test_audio_mic_mute(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--json",
            "audio", "office", "mic", "mute",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["subaction"] == "mute"
    assert parsed["muted"] is True
    assert any(c[0] == "setMicrophone" and c[2] is True for c in fake.calls)


def test_audio_mic_unmute(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--json",
            "audio", "office", "mic", "unmute",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["muted"] is False
    assert any(c[0] == "setMicrophone" and c[2] is False for c in fake.calls)


def test_audio_mic_status(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--json",
            "audio", "office", "mic", "status",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["subaction"] == "status"


# ---------------------------------------------------------------------------
# speaker
# ---------------------------------------------------------------------------


def test_audio_speaker_mute_uses_volume_zero(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--json",
            "audio", "office", "speaker", "mute",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["muted"] is True
    assert parsed["volume"] == 0
    assert ("setSpeakerVolume", 0) in fake.calls


def test_audio_speaker_unmute_restores_default_volume(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--json",
            "audio", "office", "speaker", "unmute",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["muted"] is False
    assert parsed["volume"] == 50  # default unmute volume
    assert ("setSpeakerVolume", 50) in fake.calls


def test_audio_speaker_status_reads_audio_config(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    fake.audio_config["speaker"] = {"volume": 80}
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--json",
            "audio", "office", "speaker", "status",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["volume"] == 80
    assert parsed["muted"] is False


# ---------------------------------------------------------------------------
# tts — capability-gate exit 5 on unsupported models (C200)
# ---------------------------------------------------------------------------


def test_audio_tts_on_c200_exits_5(tmp_path: Path, monkeypatch) -> None:
    """C200 has audio_tts=false → exit 5."""
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path)),
            "--json",
            "audio", "office", "tts", "hello world",
        ],
    )
    assert result.exit_code == 5, result.output
    err = json.loads(result.output.strip().splitlines()[-1])
    assert err["error"] == "unsupported_feature"
    # Hint should reference a supporting model.
    assert "C520WS" in err.get("hint", "")


def test_audio_tts_empty_text_exits_64(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake, model="C520WS")
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path, model="C520WS")),
            "--json",
            "audio", "office", "tts", "   ",
        ],
    )
    assert result.exit_code == 64, result.output


# ---------------------------------------------------------------------------
# Capability gate — TC55 has no audio at all → volume exits 5
# ---------------------------------------------------------------------------


def test_audio_volume_on_no_speaker_model_exits_5(tmp_path: Path, monkeypatch) -> None:
    """TC55 has audio_speaker=false → exit 5 even on a valid 0..100 value."""
    fake = _FakeTapo()
    fake.basic_info = {"device_info": {"basic_info": {"device_model": "TC55"}}}
    _patch_connect(monkeypatch, fake, model="TC55")
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(_cfg(tmp_path, model="TC55")),
            "--json",
            "audio", "office", "volume", "50",
        ],
    )
    assert result.exit_code == 5, result.output
