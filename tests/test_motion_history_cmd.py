"""Tests for ``tapo-cli motion history <target>`` (Phase 3, FR-25..25d, B8).

Mock-only — the camera surface is :meth:`Tapo.getEvents`.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from tapo_cli import auth_cache
from tapo_cli.cli import main
from tapo_cli.wrapper import TapoConnection, TapoTarget


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(auth_cache.ENV_CONFIG_DIR, str(tmp_path / "tapo-cli"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TAPO_CLI_CONFIG", raising=False)


def _cfg(tmp_path: Path) -> Path:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[devices.office]\nip = "192.168.1.11"\nmac = "AA:BB:CC:DD:EE:02"\n',
        encoding="utf-8",
    )
    return cfg_path


def _now_epoch() -> float:
    return dt.datetime.now(tz=dt.UTC).timestamp()


class _FakeTapo:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.last_call_kwargs: dict[str, Any] = {}

    def getEvents(  # noqa: N802 — pytapo API name
        self,
        startTime: float | bool = False,  # noqa: N803 — pytapo API name
        endTime: float | bool = False,  # noqa: N803 — pytapo API name
    ) -> list[dict[str, Any]]:
        self.last_call_kwargs = {"startTime": startTime, "endTime": endTime}
        return list(self.events)


def _patch_connect(monkeypatch, tapo: _FakeTapo) -> None:
    async def _fake_connect(cfg, target, *, credential_source=None, timeout=5.0):
        return TapoConnection(
            tapo=tapo,
            target=TapoTarget(alias="office", ip="192.168.1.11"),
            credential_family="camera_account",
        )

    monkeypatch.setattr("tapo_cli.wrapper.connect", _fake_connect)


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_history_jsonl_default_emits_one_per_line(tmp_path: Path, monkeypatch) -> None:
    now = _now_epoch()
    events = [
        {"start_time": now - 60, "end_time": now - 50, "type": 1, "region": "full"},
        {"start_time": now - 600, "end_time": now - 590, "type": 7, "region": "full"},
    ]
    fake = _FakeTapo(events)
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "motion", "history", "office"]
    )
    assert result.exit_code == 0, result.output
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    # FR-25c: ascending sort by ts.
    assert parsed[0]["ts"] < parsed[1]["ts"]
    # FR-25a: target/ts/event_type/region/has_clip present.
    for row in parsed:
        for k in ("target", "ts", "event_type", "region", "has_clip"):
            assert k in row, row
        assert row["target"] == "office"
        assert row["ts"].endswith("Z")  # B8: RFC 3339 UTC


def test_history_event_type_classification(tmp_path: Path, monkeypatch) -> None:
    now = _now_epoch()
    events = [
        {"start_time": now - 60, "type": 1},   # motion
        {"start_time": now - 50, "type": 7},   # person
        {"start_time": now - 40, "type": 11},  # doorbell-press
        {"start_time": now - 30, "type": "vehicle"},
        {"start_time": now - 20, "type": 99},  # unknown
    ]
    fake = _FakeTapo(events)
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "--json", "motion", "history", "office"]
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    types = [r["event_type"] for r in parsed]
    assert "motion" in types
    assert "person" in types
    assert "doorbell-press" in types
    assert "vehicle" in types
    assert "unknown" in types


def test_history_event_type_filter(tmp_path: Path, monkeypatch) -> None:
    now = _now_epoch()
    events = [
        {"start_time": now - 60, "type": 1},   # motion
        {"start_time": now - 50, "type": 11},  # doorbell-press
        {"start_time": now - 40, "type": 7},   # person
    ]
    fake = _FakeTapo(events)
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
            "--event-type",
            "doorbell-press",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert len(parsed) == 1
    assert parsed[0]["event_type"] == "doorbell-press"


def test_history_limit_truncates_after_sort(tmp_path: Path, monkeypatch) -> None:
    now = _now_epoch()
    events = [
        {"start_time": now - i * 60, "type": 1} for i in range(1, 11)
    ]
    fake = _FakeTapo(events)
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
            "--limit",
            "3",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert len(parsed) == 3
    # After ascending sort, the 3 retained are the OLDEST events.
    timestamps = [r["ts"] for r in parsed]
    assert timestamps == sorted(timestamps)


# ---------------------------------------------------------------------------
# --since handling (FR-25b, B8)
# ---------------------------------------------------------------------------


def test_history_since_rfc3339_with_z(tmp_path: Path, monkeypatch) -> None:
    now = _now_epoch()
    events = [
        {"start_time": now - 60, "type": 1},
        {"start_time": now - 86400 * 2, "type": 1},  # 2d ago — should be excluded
    ]
    fake = _FakeTapo(events)
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    one_hour_ago_iso = (
        dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=1)
    ).isoformat().replace("+00:00", "Z")
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--json",
            "motion",
            "history",
            "office",
            "--since",
            one_hour_ago_iso,
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert len(parsed) == 1


def test_history_since_relative_shorthand(tmp_path: Path, monkeypatch) -> None:
    now = _now_epoch()
    events = [{"start_time": now - 30, "type": 1}]
    fake = _FakeTapo(events)
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
            "--since",
            "1h",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert len(parsed) == 1


def test_history_since_bare_date(tmp_path: Path, monkeypatch) -> None:
    """FR-25b: ``YYYY-MM-DD`` is treated as ``T00:00:00Z``."""
    now = _now_epoch()
    events = [{"start_time": now - 30, "type": 1}]
    fake = _FakeTapo(events)
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    yesterday = (
        dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=1)
    ).date().isoformat()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--json",
            "motion",
            "history",
            "office",
            "--since",
            yesterday,
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert len(parsed) == 1


def test_history_since_in_future_returns_empty_exit_zero(
    tmp_path: Path, monkeypatch
) -> None:
    """FR-25d: a future --since exits 0 with empty output."""
    fake = _FakeTapo([{"start_time": 99999999999, "type": 1}])
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    far_future = "2099-01-01T00:00:00Z"
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--json",
            "motion",
            "history",
            "office",
            "--since",
            far_future,
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed == []


def test_history_since_invalid_exits_64(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo([])
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
            "--since",
            "garbage-not-a-date",
        ],
    )
    assert result.exit_code == 64, result.output


def test_history_limit_zero_exits_64(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo([])
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
            "--limit",
            "0",
        ],
    )
    assert result.exit_code == 64, result.output


# ---------------------------------------------------------------------------
# Sort determinism (FR-25c)
# ---------------------------------------------------------------------------


def test_history_results_sorted_ascending_by_ts(
    tmp_path: Path, monkeypatch
) -> None:
    now = _now_epoch()
    # Out-of-order input on purpose.
    events = [
        {"start_time": now - 100, "type": 1},
        {"start_time": now - 500, "type": 1},
        {"start_time": now - 300, "type": 1},
    ]
    fake = _FakeTapo(events)
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--json", "motion", "history", "office"],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    timestamps = [r["ts"] for r in parsed]
    assert timestamps == sorted(timestamps)
