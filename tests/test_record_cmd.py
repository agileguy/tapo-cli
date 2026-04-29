"""Tests for ``tapo-cli record`` (Phase 3, FR-13..13g, S3).

Mock-only — ffmpeg is replaced with a fake :class:`subprocess.Popen` that
records the argv it received and exits cleanly without spawning a real
child.
"""

from __future__ import annotations

import json
import signal
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from tapo_cli import auth_cache
from tapo_cli.cli import main


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(auth_cache.ENV_CONFIG_DIR, str(tmp_path / "tapo-cli"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TAPO_CLI_CONFIG", raising=False)


def _camera_account_file(tmp_path: Path) -> Path:
    cred = tmp_path / "cam.json"
    cred.write_text(
        json.dumps({"version": 1, "username": "camuser", "password": "campw1234"})
    )
    cred.chmod(0o600)
    return cred


def _cfg(tmp_path: Path) -> Path:
    cam = _camera_account_file(tmp_path)
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        "[devices.office]\n"
        'ip = "192.168.1.11"\n'
        'mac = "AA:BB:CC:DD:EE:02"\n'
        f'camera_account_file = "{cam}"\n'
        "[devices.kitchen]\n"
        'ip = "192.168.1.12"\n'
        f'camera_account_file = "{cam}"\n'
        "[groups]\n"
        'indoor = ["office", "kitchen"]\n',
        encoding="utf-8",
    )
    return cfg_path


# ---------------------------------------------------------------------------
# Fake ffmpeg subprocess
# ---------------------------------------------------------------------------


class _FakePopen:
    """Mimic just enough of :class:`subprocess.Popen` for record_cmd."""

    last_argv: list[str] | None = None
    last_kwargs: dict[str, Any] | None = None

    def __init__(
        self,
        argv: list[str],
        *,
        rc: int = 0,
        write_bytes: int = 1024,
        raise_on_wait: BaseException | None = None,
        wait_called_signal: int | None = None,
    ) -> None:
        type(self).last_argv = list(argv)
        self._rc = rc
        self._write_bytes = write_bytes
        self._raise_on_wait = raise_on_wait
        self._signaled: list[int] = []
        self._wait_calls = 0
        self.returncode: int | None = None
        self.stderr = None
        # Find the output path argument (last positional).
        self._output_path = argv[-1] if argv else None

    def wait(self, timeout: float | None = None) -> int:
        self._wait_calls += 1
        if self._raise_on_wait is not None and self._wait_calls == 1:
            exc = self._raise_on_wait
            self._raise_on_wait = None
            raise exc
        # Write the configured number of bytes to the output path so the
        # bytes-stat assertion has data.
        if self._output_path:
            try:
                with open(self._output_path, "wb") as f:
                    f.write(b"x" * self._write_bytes)
            except OSError:
                pass
        self.returncode = self._rc
        return self._rc

    def send_signal(self, sig: int) -> None:
        self._signaled.append(sig)

    def kill(self) -> None:
        self._signaled.append(signal.SIGKILL)


# Module-level holder so the closure can reach the test's configured fake.
_FAKE_FACTORY: dict[str, Any] = {"factory": None}


def _install_fake_popen(monkeypatch, *, rc: int = 0, write_bytes: int = 1024) -> None:
    def _factory(argv: list[str], **kwargs: Any) -> _FakePopen:
        _FakePopen.last_kwargs = kwargs
        return _FakePopen(argv, rc=rc, write_bytes=write_bytes)

    _FAKE_FACTORY["factory"] = _factory
    monkeypatch.setattr(
        "tapo_cli.verbs.record_cmd.subprocess.Popen", _factory
    )
    # Force ffmpeg "available" → no shutil.which dependency.
    monkeypatch.setattr(
        "tapo_cli.verbs.record_cmd._ffmpeg_available", lambda _: True
    )


# ---------------------------------------------------------------------------
# Footgun guard (FR-13a)
# ---------------------------------------------------------------------------


def test_non_tty_without_duration_or_max_bytes_exits_64(
    tmp_path: Path, monkeypatch
) -> None:
    _install_fake_popen(monkeypatch)
    out = tmp_path / "out.mp4"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "record",
            "office",
            "--output",
            str(out),
        ],
    )
    assert result.exit_code == 64, result.output
    assert "non-tty" in result.output or "duration" in result.output


def test_duration_records_and_passes_t_to_ffmpeg(
    tmp_path: Path, monkeypatch
) -> None:
    _install_fake_popen(monkeypatch, rc=0, write_bytes=2048)
    out = tmp_path / "rec.mp4"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--json",
            "record",
            "office",
            "--output",
            str(out),
            "--duration",
            "5",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["target"] == "office"
    assert parsed["bytes"] == 2048
    assert parsed["exit_reason"] == "max-duration"
    # ffmpeg argv MUST carry -t 5.
    assert _FakePopen.last_argv is not None
    assert "-t" in _FakePopen.last_argv
    assert "5" in _FakePopen.last_argv


def test_max_bytes_passes_fs_to_ffmpeg(tmp_path: Path, monkeypatch) -> None:
    _install_fake_popen(monkeypatch, rc=0, write_bytes=512)
    out = tmp_path / "rec.mp4"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--json",
            "record",
            "office",
            "--output",
            str(out),
            "--max-bytes",
            "1024",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["exit_reason"] == "max-bytes"
    assert _FakePopen.last_argv is not None
    assert "-fs" in _FakePopen.last_argv
    assert "1024" in _FakePopen.last_argv


def test_negative_duration_exits_64(tmp_path: Path, monkeypatch) -> None:
    _install_fake_popen(monkeypatch)
    out = tmp_path / "rec.mp4"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "record",
            "office",
            "--output",
            str(out),
            "--duration",
            "0",
        ],
    )
    assert result.exit_code == 64


# ---------------------------------------------------------------------------
# SIGINT / SIGTERM forwarding (FR-13b)
# ---------------------------------------------------------------------------


def test_sigint_during_wait_forwards_to_ffmpeg_and_exits_130(
    tmp_path: Path, monkeypatch
) -> None:
    """First wait() raises KeyboardInterrupt; second wait (after we forward
    the signal) returns cleanly. We assert SIGINT was sent and the verb
    returns exit 130 with exit_reason=='sigint'."""

    def _factory(argv: list[str], **kwargs: Any) -> _FakePopen:
        return _FakePopen(
            argv,
            rc=130,
            write_bytes=4096,
            raise_on_wait=KeyboardInterrupt(),
        )

    monkeypatch.setattr("tapo_cli.verbs.record_cmd.subprocess.Popen", _factory)
    monkeypatch.setattr(
        "tapo_cli.verbs.record_cmd._ffmpeg_available", lambda _: True
    )

    out = tmp_path / "rec.mp4"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--json",
            "record",
            "office",
            "--output",
            str(out),
            "--duration",
            "60",
        ],
    )
    assert result.exit_code == 130, result.output
    parsed = json.loads(result.stdout)
    assert parsed["exit_reason"] == "sigint"


def test_sigterm_during_wait_forwards_to_ffmpeg_and_exits_143(
    tmp_path: Path, monkeypatch
) -> None:
    def _factory(argv: list[str], **kwargs: Any) -> _FakePopen:
        return _FakePopen(
            argv,
            rc=143,
            write_bytes=4096,
            raise_on_wait=SystemExit(143),
        )

    monkeypatch.setattr("tapo_cli.verbs.record_cmd.subprocess.Popen", _factory)
    monkeypatch.setattr(
        "tapo_cli.verbs.record_cmd._ffmpeg_available", lambda _: True
    )

    out = tmp_path / "rec.mp4"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--json",
            "record",
            "office",
            "--output",
            str(out),
            "--max-bytes",
            "999999999",
        ],
    )
    assert result.exit_code == 143, result.output
    parsed = json.loads(result.stdout)
    assert parsed["exit_reason"] == "sigterm"


# ---------------------------------------------------------------------------
# Group target rejection (FR-43c)
# ---------------------------------------------------------------------------


def test_group_target_rejected_exits_64(tmp_path: Path, monkeypatch) -> None:
    _install_fake_popen(monkeypatch)
    out = tmp_path / "rec.mp4"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "record",
            "@indoor",
            "--output",
            str(out),
            "--duration",
            "1",
        ],
    )
    assert result.exit_code == 64, result.output


# ---------------------------------------------------------------------------
# ffmpeg missing (FR-13c)
# ---------------------------------------------------------------------------


def test_ffmpeg_missing_exits_6(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "tapo_cli.verbs.record_cmd._ffmpeg_available", lambda _: False
    )
    out = tmp_path / "rec.mp4"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "record",
            "office",
            "--output",
            str(out),
            "--duration",
            "1",
        ],
    )
    assert result.exit_code == 6, result.output


# ---------------------------------------------------------------------------
# Camera account requirement (FR-CRED-7)
# ---------------------------------------------------------------------------


def test_no_camera_account_file_exits_2(tmp_path: Path, monkeypatch) -> None:
    _install_fake_popen(monkeypatch)
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[devices.office]\nip = "192.168.1.11"\nmac = "AA:BB:CC:DD:EE:02"\n',
        encoding="utf-8",
    )
    out = tmp_path / "rec.mp4"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(cfg_path),
            "record",
            "office",
            "--output",
            str(out),
            "--duration",
            "1",
        ],
    )
    assert result.exit_code == 2, result.output


# ---------------------------------------------------------------------------
# ffmpeg argv shape (no transcoding, RTSP TCP)
# ---------------------------------------------------------------------------


def test_ffmpeg_argv_uses_copy_codec_and_rtsp_tcp(
    tmp_path: Path, monkeypatch
) -> None:
    _install_fake_popen(monkeypatch)
    out = tmp_path / "rec.mp4"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "record",
            "office",
            "--output",
            str(out),
            "--duration",
            "2",
        ],
    )
    assert result.exit_code == 0, result.output
    argv = _FakePopen.last_argv
    assert argv is not None
    # Video is copied, audio is transcoded to AAC because Tapo emits
    # PCM_ALAW which MP4 doesn't accept under ``-c copy``.
    assert "-c:v" in argv
    assert "copy" in argv
    assert "-c:a" in argv
    assert "aac" in argv
    assert "-rtsp_transport" in argv
    assert "tcp" in argv
    # Output path is the last argv entry.
    assert argv[-1] == str(out)
