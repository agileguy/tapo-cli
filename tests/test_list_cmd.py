"""Tests for ``tapo-cli list`` (FR-6, FR-6a, FR-6b, FR-8)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from tapo_cli.cli import main
from tapo_cli.verbs import list_cmd as list_module


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TAPO_CLI_CONFIG", raising=False)


def _cfg(tmp_path: Path) -> Path:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        """
[devices.front-door]
ip = "192.168.1.10"
mac = "AA:BB:CC:DD:EE:01"
model = "C220"

[devices.office]
ip = "192.168.1.11"
mac = "AA:BB:CC:DD:EE:02"
model = "C200"
""".lstrip(),
        encoding="utf-8",
    )
    return cfg_path


def test_list_dumps_aliases(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--config", str(_cfg(tmp_path)), "--json", "list"])
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert {row["alias"] for row in parsed} == {"front-door", "office"}
    # Without --probe, online MUST be null (FR-6b).
    assert all(row["online"] is None for row in parsed)


def test_probe_adds_online_field(tmp_path: Path, monkeypatch) -> None:
    async def _fake_probe(ip: str, *, timeout: float) -> bool:
        return ip == "192.168.1.10"  # only front-door responds

    monkeypatch.setattr(list_module, "_probe_alive", _fake_probe)

    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "--json", "list", "--probe"]
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    by_alias = {row["alias"]: row for row in parsed}
    assert by_alias["front-door"]["online"] is True
    assert by_alias["office"]["online"] is False


def test_online_only_suppresses_offline_entries(tmp_path: Path, monkeypatch) -> None:
    async def _fake_probe(ip: str, *, timeout: float) -> bool:
        return ip == "192.168.1.10"

    monkeypatch.setattr(list_module, "_probe_alive", _fake_probe)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--json", "list", "--online-only"],
    )
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert len(parsed) == 1
    assert parsed[0]["alias"] == "front-door"
    assert parsed[0]["online"] is True


def test_no_devices_emits_empty_array(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[defaults]\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(main, ["--config", str(cfg_path), "--json", "list"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == []


def test_jsonl_default_for_non_tty(tmp_path: Path) -> None:
    """FR-46: stdout is non-tty in CliRunner → default mode is JSONL."""
    runner = CliRunner()
    result = runner.invoke(main, ["--config", str(_cfg(tmp_path)), "list"])
    assert result.exit_code == 0
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(lines) == 2
    # Each line MUST round-trip JSON.
    aliases = {json.loads(ln)["alias"] for ln in lines}
    assert aliases == {"front-door", "office"}
