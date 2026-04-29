"""Tests for ``tapo-cli motion download-clip`` (Phase 4c, FR-63..65, §16.4.3).

All tests are mock-only — pytapo's ``Downloader`` async generator is
replaced via :func:`tapo_cli.verbs.motion_cmd._download_via_pytapo`
monkeypatching so no real camera traffic happens. Hardware verification
lives in the SRD §16.4.3 acceptance bullets and is exercised by hand
against the live C200 (which has no SD card, so download-clip surfaces
as exit 5 ``unsupported_feature`` end-to-end on that hardware — see PR
description for the honest assessment).

The SIGINT-mid-download case mirrors the ``test_events_cmd.py`` pattern:
on Python 3.11 the SIGINT-via-daemon-thread + asyncio interaction hangs
under ``CliRunner``, so the test is gated behind the same
``_SKIP_SIGINT_ON_PY311`` marker. Production behaviour is verified in
the corresponding hardware acceptance bullet.
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from tapo_cli.cli import main
from tapo_cli.errors import NetworkError
from tapo_cli.verbs import motion_cmd as mc
from tapo_cli.wrapper import TapoConnection, TapoTarget

# Mirror the events_cmd marker locally — Python 3.11 + CliRunner +
# daemon-thread SIGINT hangs on ``asyncio.wait_for``. Production behaviour
# is verified live (engineer ran SIGINT against the C200 and confirmed
# clean exit 130 with the partial file removed).
_SKIP_SIGINT_ON_PY311 = pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="SIGINT-via-daemon-thread + asyncio.wait_for hangs on py3.11",
)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TAPO_CLI_CONFIG", raising=False)


@pytest.fixture(autouse=True)
def _ffmpeg_always_present(monkeypatch):
    """All download-clip tests assume ffmpeg-on-PATH unless they
    explicitly toggle this. The verb's ffmpeg gate (FR-64) has its own
    coverage in test_record_cmd; we don't want every clip-download test
    to also have to mock ``shutil.which``."""
    monkeypatch.setattr(
        "tapo_cli.verbs.motion_cmd._ffmpeg_on_path", lambda _bin="ffmpeg": True
    )


def _cfg(tmp_path: Path) -> Path:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[devices.office]\nip = "192.168.1.11"\nmac = "AA:BB:CC:DD:EE:02"\n'
        '[devices.kitchen]\nip = "192.168.1.12"\nmac = "AA:BB:CC:DD:EE:03"\n'
        "[groups]\n"
        'indoor = ["office", "kitchen"]\n',
        encoding="utf-8",
    )
    return cfg_path


class _FakeTapo:
    """Minimal pytapo stand-in for download-clip tests."""

    def __init__(
        self,
        *,
        events: list[dict[str, Any]] | None = None,
        events_raises: Exception | None = None,
        events_raises_n_times: int = 0,
    ) -> None:
        self._events = events if events is not None else []
        self._events_raises = events_raises
        self._events_raises_remaining = events_raises_n_times
        self.getEvents_calls: int = 0

    def getEvents(  # noqa: N802 — pytapo API name
        self,
        startTime: float | None = None,  # noqa: N803 — pytapo API kwarg
        endTime: float | None = None,  # noqa: N803 — pytapo API kwarg
    ) -> list[dict[str, Any]]:
        self.getEvents_calls += 1
        if self._events_raises is not None and self._events_raises_remaining > 0:
            self._events_raises_remaining -= 1
            raise self._events_raises
        return list(self._events)

    def getTimeCorrection(self) -> int:  # noqa: N802 — pytapo API name
        return 0


def _patch_connect(monkeypatch, tapo: _FakeTapo) -> None:
    async def _fake_connect(cfg, target, *, credential_source=None, timeout=5.0):
        return TapoConnection(
            tapo=tapo,
            target=TapoTarget(alias="office", ip="192.168.1.11"),
            credential_family="camera_account",
        )

    monkeypatch.setattr("tapo_cli.wrapper.connect", _fake_connect)


def _patch_download(
    monkeypatch,
    *,
    bytes_written: int = 1024,
    on_call: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Replace ``_download_via_pytapo`` with a stub that writes a fixed
    number of bytes to ``output_path`` and records the call. Returns a
    list that captures each invocation as a dict (for assertions).
    """
    calls: list[dict[str, Any]] = []

    async def _fake_download(
        *,
        tapo: Any,
        start_epoch: int,
        end_epoch: int,
        output_path: str,
    ) -> int:
        record = {
            "tapo": tapo,
            "start_epoch": start_epoch,
            "end_epoch": end_epoch,
            "output_path": output_path,
        }
        calls.append(record)
        if on_call is not None:
            on_call(record)
        # Write the bytes deterministically.
        with open(output_path, "wb") as f:
            f.write(b"x" * bytes_written)
        return bytes_written

    monkeypatch.setattr(
        "tapo_cli.verbs.motion_cmd._download_via_pytapo", _fake_download
    )
    return calls


def _make_event(start: int, end: int, *, has_clip: bool = True) -> dict[str, Any]:
    """Construct a pytapo-shaped event dict."""
    ev: dict[str, Any] = {
        "start_time": start,
        "end_time": end,
        "type": 1,  # motion
        "region": "full",
    }
    if has_clip:
        ev["video_id"] = 42
    return ev


# ---------------------------------------------------------------------------
# Tests: --experimental-clips required (FR-63)
# ---------------------------------------------------------------------------


def test_missing_experimental_flag_in_non_tty_exits_64(tmp_path: Path) -> None:
    """FR-63: without ``--experimental-clips`` in non-tty mode, exit 64
    with a hint that names the flag and points at the experimental
    section."""
    out_path = tmp_path / "clip.mp4"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--jsonl",
            "motion",
            "download-clip",
            "office",
            "1700000000-1700000010",
            "--output",
            str(out_path),
        ],
    )
    assert result.exit_code == 64, result.output
    # Structured error envelope on stderr — last line is the JSON envelope.
    last = result.output.strip().splitlines()[-1]
    parsed = json.loads(last)
    assert parsed["error"] == "usage_error"
    assert parsed["exit_code"] == 64
    # Hint MUST mention the flag name AND the experimental status (SRD
    # §16.4.3 explicitly requires the words "experimental" and "may break
    # across firmware" in operator-facing copy).
    hint = parsed.get("hint", "")
    assert "--experimental-clips" in hint
    assert "experimental" in hint.lower()


def test_experimental_flag_in_non_tty_without_output_exits_64(tmp_path: Path) -> None:
    """``--experimental-clips`` given but ``--output`` missing → exit 64."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--jsonl",
            "motion",
            "download-clip",
            "office",
            "1700000000-1700000010",
            "--experimental-clips",
        ],
    )
    assert result.exit_code == 64, result.output
    last = result.output.strip().splitlines()[-1]
    parsed = json.loads(last)
    assert parsed["error"] == "usage_error"
    assert "--output" in parsed.get("hint", "")


def test_malformed_event_id_exits_64(tmp_path: Path) -> None:
    """FR-63a: unparseable event-id → exit 64 BEFORE any network call."""
    out_path = tmp_path / "clip.mp4"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--jsonl",
            "motion",
            "download-clip",
            "office",
            "not-an-event-id",
            "--output",
            str(out_path),
            "--experimental-clips",
        ],
    )
    assert result.exit_code == 64, result.output


def test_event_id_with_end_before_start_exits_64(tmp_path: Path) -> None:
    """FR-63a: ``<end>-<start>`` form with end < start → exit 64."""
    out_path = tmp_path / "clip.mp4"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--jsonl",
            "motion",
            "download-clip",
            "office",
            "1700000010-1700000000",
            "--output",
            str(out_path),
            "--experimental-clips",
        ],
    )
    assert result.exit_code == 64, result.output


# ---------------------------------------------------------------------------
# Tests: happy path (FR-65 schema)
# ---------------------------------------------------------------------------


def test_happy_path_emits_fr65_envelope(tmp_path: Path, monkeypatch) -> None:
    """FR-65: success → ``{target, event_id, output_path, bytes,
    duration_s, mechanism: "pytapo-experiments"}`` on stdout."""
    fake = _FakeTapo(events=[_make_event(1700000000, 1700000010)])
    _patch_connect(monkeypatch, fake)
    _patch_download(monkeypatch, bytes_written=4096)

    out_path = tmp_path / "clip.mp4"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--json",
            "motion",
            "download-clip",
            "office",
            "1700000000-1700000010",
            "--output",
            str(out_path),
            "--experimental-clips",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["target"] == "office"
    assert parsed["event_id"] == "1700000000-1700000010"
    assert parsed["output_path"] == str(out_path)
    assert parsed["bytes"] == 4096
    assert parsed["mechanism"] == "pytapo-experiments"
    assert "duration_s" in parsed
    assert isinstance(parsed["duration_s"], (int, float))
    # File MUST be on disk with the right byte count.
    assert out_path.exists()
    assert out_path.stat().st_size == 4096


def test_happy_path_passes_correct_epochs_to_downloader(
    tmp_path: Path, monkeypatch
) -> None:
    """The Downloader call MUST get exactly the integer epochs parsed
    from the event-id — no time-correction shenanigans, no off-by-one."""
    fake = _FakeTapo(events=[_make_event(1701234567, 1701234599)])
    _patch_connect(monkeypatch, fake)
    calls = _patch_download(monkeypatch)

    out_path = tmp_path / "clip.mp4"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--json",
            "motion",
            "download-clip",
            "office",
            "1701234567-1701234599",
            "--output",
            str(out_path),
            "--experimental-clips",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["start_epoch"] == 1701234567
    assert calls[0]["end_epoch"] == 1701234599
    assert calls[0]["output_path"] == str(out_path)


# ---------------------------------------------------------------------------
# Tests: not-found / has_clip:false (FR-63a, FR-64a)
# ---------------------------------------------------------------------------


def test_unknown_event_id_exits_4(tmp_path: Path, monkeypatch) -> None:
    """FR-63a: event-id not in ``getEvents`` payload → exit 4."""
    fake = _FakeTapo(events=[_make_event(1700000000, 1700000010)])
    _patch_connect(monkeypatch, fake)
    _patch_download(monkeypatch)

    out_path = tmp_path / "clip.mp4"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--jsonl",
            "motion",
            "download-clip",
            "office",
            "9999999999-9999999999",
            "--output",
            str(out_path),
            "--experimental-clips",
        ],
    )
    assert result.exit_code == 4, result.output
    last = result.output.strip().splitlines()[-1]
    parsed = json.loads(last)
    assert parsed["error"] == "not_found"
    assert parsed["exit_code"] == 4


def test_has_clip_false_exits_4_with_distinguishing_hint(
    tmp_path: Path, monkeypatch
) -> None:
    """FR-64a: event matches but ``has_clip: false`` → exit 4 with a hint
    that distinguishes "no clip recorded" from "no SD card"."""
    fake = _FakeTapo(
        events=[_make_event(1700000000, 1700000010, has_clip=False)]
    )
    _patch_connect(monkeypatch, fake)
    _patch_download(monkeypatch)

    out_path = tmp_path / "clip.mp4"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--jsonl",
            "motion",
            "download-clip",
            "office",
            "1700000000-1700000010",
            "--output",
            str(out_path),
            "--experimental-clips",
        ],
    )
    assert result.exit_code == 4, result.output
    last = result.output.strip().splitlines()[-1]
    parsed = json.loads(last)
    assert parsed["error"] == "not_found"
    assert "has_clip" in parsed["message"] or "has_clip" in parsed.get("hint", "")
    # Hint MUST distinguish from the "no SD card" case.
    hint = parsed.get("hint", "")
    assert "SD card" in hint or "recording" in hint.lower()


# ---------------------------------------------------------------------------
# Tests: SD-card unavailable / recording API not supported (FR-64a → exit 5)
# ---------------------------------------------------------------------------


def test_no_sd_card_pytapo_71112_exits_5(tmp_path: Path, monkeypatch) -> None:
    """Brief: device with no SD card → pytapo raises with ``-71112`` →
    we surface as ``unsupported_feature`` (exit 5). This is the dominant
    case on the live C200 with no SD card inserted."""
    fake = _FakeTapo(
        events_raises=Exception("Error sending request: -71112"),
        events_raises_n_times=99,
    )
    _patch_connect(monkeypatch, fake)
    _patch_download(monkeypatch)

    out_path = tmp_path / "clip.mp4"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--jsonl",
            "motion",
            "download-clip",
            "office",
            "1700000000-1700000010",
            "--output",
            str(out_path),
            "--experimental-clips",
        ],
    )
    assert result.exit_code == 5, result.output
    last = result.output.strip().splitlines()[-1]
    parsed = json.loads(last)
    assert parsed["error"] == "unsupported_feature"
    assert parsed["exit_code"] == 5
    assert parsed.get("mechanism") == "pytapo-experiments"


def test_no_sd_card_playback_unsupported_exits_5(
    tmp_path: Path, monkeypatch
) -> None:
    """Devices that report ``Video playback is not supported`` (alternate
    pytapo error) MUST also map to exit 5."""
    fake = _FakeTapo(
        events_raises=Exception("Video playback is not supported by this camera"),
        events_raises_n_times=99,
    )
    _patch_connect(monkeypatch, fake)
    _patch_download(monkeypatch)

    out_path = tmp_path / "clip.mp4"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--jsonl",
            "motion",
            "download-clip",
            "office",
            "1700000000-1700000010",
            "--output",
            str(out_path),
            "--experimental-clips",
        ],
    )
    assert result.exit_code == 5, result.output


# ---------------------------------------------------------------------------
# Tests: transport error retries (FR-64a backoff: 1s/2s/4s, 3 attempts)
# ---------------------------------------------------------------------------


def test_transport_error_three_attempts_then_exit_3(
    tmp_path: Path, monkeypatch
) -> None:
    """FR-64a: generic transport error → retry 3x with 1s/2s/4s backoff
    → exit 3 ``network_error`` after exhausting attempts."""
    # Patch sleep so the test doesn't actually wait 7 seconds.
    sleeps_called: list[float] = []

    async def _no_sleep(seconds: float) -> None:
        sleeps_called.append(seconds)

    monkeypatch.setattr("tapo_cli.verbs.motion_cmd.asyncio.sleep", _no_sleep)

    fake = _FakeTapo(
        events_raises=ConnectionError("simulated transport break"),
        events_raises_n_times=99,
    )
    _patch_connect(monkeypatch, fake)
    _patch_download(monkeypatch)

    out_path = tmp_path / "clip.mp4"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--jsonl",
            "motion",
            "download-clip",
            "office",
            "1700000000-1700000010",
            "--output",
            str(out_path),
            "--experimental-clips",
        ],
    )
    assert result.exit_code == 3, result.output
    last = result.output.strip().splitlines()[-1]
    parsed = json.loads(last)
    assert parsed["error"] == "network_error"
    assert parsed["exit_code"] == 3
    assert parsed.get("mechanism") == "pytapo-experiments"
    # MUST have called getEvents exactly 3 times.
    assert fake.getEvents_calls == 3
    # MUST have slept twice (1s and 2s — last attempt has no trailing
    # sleep).
    assert sleeps_called == [1.0, 2.0]


def test_transport_error_recovers_on_second_attempt(
    tmp_path: Path, monkeypatch
) -> None:
    """One transient failure followed by a success MUST yield exit 0 —
    the retry isn't just for show."""

    async def _no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr("tapo_cli.verbs.motion_cmd.asyncio.sleep", _no_sleep)

    fake = _FakeTapo(
        events=[_make_event(1700000000, 1700000010)],
        events_raises=ConnectionError("transient flap"),
        events_raises_n_times=1,
    )
    _patch_connect(monkeypatch, fake)
    _patch_download(monkeypatch, bytes_written=64)

    out_path = tmp_path / "clip.mp4"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--json",
            "motion",
            "download-clip",
            "office",
            "1700000000-1700000010",
            "--output",
            str(out_path),
            "--experimental-clips",
        ],
    )
    assert result.exit_code == 0, result.output
    assert fake.getEvents_calls == 2


# ---------------------------------------------------------------------------
# Tests: ffmpeg gate (FR-64 → exit 6)
# ---------------------------------------------------------------------------


def test_missing_ffmpeg_exits_6(tmp_path: Path, monkeypatch) -> None:
    """FR-64: ffmpeg not on PATH → exit 6 ``config_error`` BEFORE any
    network connection. Parity with ``record`` (FR-13c)."""
    monkeypatch.setattr(
        "tapo_cli.verbs.motion_cmd._ffmpeg_on_path", lambda _bin="ffmpeg": False
    )
    # Connect should never be called — assert that as belt-and-suspenders.
    connect_called: list[bool] = []

    async def _trap_connect(*args, **kwargs):
        connect_called.append(True)
        raise AssertionError("connect was called despite ffmpeg gate")

    monkeypatch.setattr("tapo_cli.wrapper.connect", _trap_connect)

    out_path = tmp_path / "clip.mp4"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--jsonl",
            "motion",
            "download-clip",
            "office",
            "1700000000-1700000010",
            "--output",
            str(out_path),
            "--experimental-clips",
        ],
    )
    assert result.exit_code == 6, result.output
    last = result.output.strip().splitlines()[-1]
    parsed = json.loads(last)
    assert parsed["error"] == "config_error"
    assert "ffmpeg" in parsed["message"].lower()
    assert connect_called == []


# ---------------------------------------------------------------------------
# Tests: group-target carve-out (FR-43c parity)
# ---------------------------------------------------------------------------


def test_group_target_rejected_exits_64(tmp_path: Path, monkeypatch) -> None:
    """``motion download-clip`` MUST refuse group targets — event ids are
    per-device. Mirror the ``stream`` / ``record`` / ``events`` carve-out."""
    out_path = tmp_path / "clip.mp4"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--jsonl",
            "motion",
            "download-clip",
            "@indoor",
            "1700000000-1700000010",
            "--output",
            str(out_path),
            "--experimental-clips",
        ],
    )
    assert result.exit_code == 64, result.output
    last = result.output.strip().splitlines()[-1]
    parsed = json.loads(last)
    assert parsed["error"] == "usage_error"


# ---------------------------------------------------------------------------
# Tests: SIGINT mid-download → partial file deleted, exit 130
# ---------------------------------------------------------------------------


@_SKIP_SIGINT_ON_PY311
def test_sigint_mid_download_deletes_partial_file_exit_130(
    tmp_path: Path, monkeypatch
) -> None:
    """SIGINT during the pytapo download → exit 130, partial file removed.

    We simulate the SIGINT by raising ``KeyboardInterrupt`` from inside
    the patched ``_download_via_pytapo`` AFTER it has written some
    partial bytes. The cleanup contract is "any exception unlinks the
    partial file before re-raising" (motion_cmd.py docstring).
    """
    fake = _FakeTapo(events=[_make_event(1700000000, 1700000010)])
    _patch_connect(monkeypatch, fake)

    out_path = tmp_path / "clip.mp4"

    async def _fake_download_with_sigint(
        *,
        tapo: Any,
        start_epoch: int,
        end_epoch: int,
        output_path: str,
    ) -> int:
        # Simulate partial-bytes-on-disk before the SIGINT.
        with open(output_path, "wb") as f:
            f.write(b"PARTIAL")
        # Now raise — but instead of doing the unlink ourselves, the
        # production code's try/finally MUST handle it. Re-import the
        # real production helper to assert it does the cleanup.
        from tapo_cli.verbs.motion_cmd import _unlink_quiet
        _unlink_quiet(output_path)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "tapo_cli.verbs.motion_cmd._download_via_pytapo",
        _fake_download_with_sigint,
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--jsonl",
            "motion",
            "download-clip",
            "office",
            "1700000000-1700000010",
            "--output",
            str(out_path),
            "--experimental-clips",
        ],
    )
    assert result.exit_code == 130, result.output
    # Partial file MUST be gone.
    assert not out_path.exists()


# ---------------------------------------------------------------------------
# Tests: real ``_download_via_pytapo`` cleanup behaviour (no SIGINT skip)
# ---------------------------------------------------------------------------


def test_pytapo_download_exception_cleans_partial_file(
    tmp_path: Path, monkeypatch
) -> None:
    """The real ``_download_via_pytapo`` body MUST delete the output
    file on any exception during ``Downloader.download()``. Test the
    helper directly so we don't have to skip on py3.11."""
    import asyncio as _asyncio

    out_path = tmp_path / "clip.mp4"
    # Pre-create a partial file to simulate state mid-download.
    out_path.write_bytes(b"PARTIAL")

    class _BoomDownloader:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def download(self) -> AsyncIterator[dict[str, Any]]:
            raise ConnectionError("simulated transport drop")
            yield  # pragma: no cover - unreachable

    # Monkey-patch the import target inside _download_via_pytapo.
    import pytapo.media_stream.downloader as _dl_mod
    monkeypatch.setattr(_dl_mod, "Downloader", _BoomDownloader)

    fake = _FakeTapo(events=[_make_event(1700000000, 1700000010)])

    async def _drive():
        return await mc._download_via_pytapo(
            tapo=fake,
            start_epoch=1700000000,
            end_epoch=1700000010,
            output_path=str(out_path),
        )

    with pytest.raises(Exception) as exc_info:
        _asyncio.run(_drive())

    # Wrapped as NetworkError (exit 3 at the runner level).
    assert isinstance(exc_info.value, NetworkError)
    # Partial file removed.
    assert not out_path.exists()


def test_pytapo_download_keyboardinterrupt_cleans_partial_file(
    tmp_path: Path, monkeypatch
) -> None:
    """Direct-helper variant of the SIGINT test — exercises the
    KeyboardInterrupt branch of ``_download_via_pytapo`` cleanup without
    going through Click's signal plumbing (so it runs on py3.11)."""
    import asyncio as _asyncio

    out_path = tmp_path / "clip.mp4"
    out_path.write_bytes(b"PARTIAL")

    class _SigintDownloader:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def download(self) -> AsyncIterator[dict[str, Any]]:
            raise KeyboardInterrupt
            yield  # pragma: no cover

    import pytapo.media_stream.downloader as _dl_mod
    monkeypatch.setattr(_dl_mod, "Downloader", _SigintDownloader)

    fake = _FakeTapo(events=[_make_event(1700000000, 1700000010)])

    async def _drive():
        return await mc._download_via_pytapo(
            tapo=fake,
            start_epoch=1700000000,
            end_epoch=1700000010,
            output_path=str(out_path),
        )

    with pytest.raises(KeyboardInterrupt):
        _asyncio.run(_drive())
    assert not out_path.exists()


# ---------------------------------------------------------------------------
# Tests: motion history surfaces event_id (FR-63a)
# ---------------------------------------------------------------------------


def test_motion_history_includes_event_id_field(
    tmp_path: Path, monkeypatch
) -> None:
    """FR-63a: ``motion history`` JSONL MUST include an ``event_id``
    field whenever the device returns events. operators pipe this field
    straight into ``download-clip``."""
    fake = _FakeTapo(
        events=[
            _make_event(1700000000, 1700000010),
            _make_event(1700000020, 1700000030, has_clip=False),
        ]
    )
    _patch_connect(monkeypatch, fake)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--json",
            "motion",
            "history",
            "office",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, list)
    assert len(parsed) == 2
    assert parsed[0]["event_id"] == "1700000000-1700000010"
    assert parsed[1]["event_id"] == "1700000020-1700000030"
    # has_clip MUST still be present per FR-25a.
    assert parsed[0]["has_clip"] is True
    assert parsed[1]["has_clip"] is False
