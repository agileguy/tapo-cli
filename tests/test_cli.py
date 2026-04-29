"""Tests for the top-level Click app surface (Phase 1a)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

import tapo_cli
from tapo_cli import auth_cache
from tapo_cli.cli import main

PACKAGE_VERSION = tapo_cli.__version__


@pytest.fixture(autouse=True)
def _redirect_cache(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(auth_cache.ENV_CONFIG_DIR, str(tmp_path / "tapo-cli"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TAPO_CLI_CONFIG", raising=False)


# ---------------------------------------------------------------------------
# --help / --version
# ---------------------------------------------------------------------------


def test_top_level_help_lists_phase_1c_verbs() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0, result.output
    out = result.output
    # Phase 1a foundation verbs.
    assert "auth" in out
    assert "config" in out
    # Phase 1b: the three read-only discovery verbs MUST be exposed.
    for required in ("discover", "list", "info"):
        assert required in out, (
            f"Phase 1b CLI must list verb {required!r} in --help"
        )
    # Phase 1c: snapshot + stream MUST now be exposed.
    for required in ("snapshot", "stream"):
        assert required in out, (
            f"Phase 1c CLI must list verb {required!r} in --help"
        )
    # Phase 1d / Phase 2+ camera verbs are still embargoed.
    for forbidden in (
        "record",
        "ptz",
        "preset",
        "motion",
        "alarm",
        "led",
        "privacy",
        "night-vision",
        "audio",
        "osd",
        "reboot",
        "batch",
    ):
        assert forbidden not in out, (
            f"Phase 1d/2 verb {forbidden!r} must not appear yet"
        )


def test_version_emits_package_version() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert PACKAGE_VERSION in result.output
    assert PACKAGE_VERSION == "0.1.2"


def test_auth_help_lists_three_actions() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["auth", "--help"])
    assert result.exit_code == 0
    assert "status" in result.output
    assert "flush" in result.output
    assert "migrate" in result.output


def test_config_help_lists_two_actions() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["config", "--help"])
    assert result.exit_code == 0
    assert "show" in result.output
    assert "validate" in result.output


# ---------------------------------------------------------------------------
# Usage errors → exit 64
# ---------------------------------------------------------------------------


def test_unknown_subcommand_exits_64() -> None:
    """SRD §11.1: usage error → exit 64 (Click default 2 is wrong here).

    The __main__ shim is what translates Click's UsageError (default exit
    2) into the SRD-mandated 64. We exercise the shim directly because
    CliRunner doesn't pass through our shim.
    """
    import sys

    from tapo_cli.__main__ import main as shim_main

    saved_argv = sys.argv
    try:
        sys.argv = ["tapo-cli", "garbage"]
        code = shim_main()
    finally:
        sys.argv = saved_argv
    assert code == 64


def test_mutex_json_jsonl_exits_64() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--json", "--jsonl", "auth", "status"])
    assert result.exit_code == 64
    assert "mutually exclusive" in result.output


# ---------------------------------------------------------------------------
# auth status with zero cached devices
# ---------------------------------------------------------------------------


def test_auth_status_empty_emits_clean_json_array() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--json", "auth", "status"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed == []


def test_auth_status_empty_jsonl_is_silent() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--jsonl", "auth", "status"])
    assert result.exit_code == 0
    assert result.output.strip() == ""


def test_auth_status_text_empty_is_silent() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["auth", "status"])
    assert result.exit_code == 0
    # Empty cache → no rows.
    assert result.output.strip() == ""


# ---------------------------------------------------------------------------
# auth flush
# ---------------------------------------------------------------------------


def test_auth_flush_no_target_returns_zero() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["auth", "flush"])
    assert result.exit_code == 0
    assert "flushed 0" in result.output


def test_auth_flush_with_target_alias_resolves_via_config(tmp_path: Path) -> None:
    """alias → config-resolved MAC → file deletion."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[devices.front-door]\nip = "192.168.1.42"\nmac = "AA:BB:CC:DD:EE:01"\n',
        encoding="utf-8",
    )
    auth_cache.save_session("AA:BB:CC:DD:EE:01", {"x": 1}, pytapo_version="0.0.test")

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(cfg_path), "auth", "flush", "--target", "front-door"],
    )
    assert result.exit_code == 0
    assert "flushed 1" in result.output
    assert not auth_cache.cache_path_for_mac("AA:BB:CC:DD:EE:01").exists()


# ---------------------------------------------------------------------------
# config show / validate
# ---------------------------------------------------------------------------


def test_config_show_emits_canonical_toml(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[devices.cam1]\nip = "10.0.0.1"\nmac = "AA:BB:CC:DD:EE:01"\n',
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(main, ["--config", str(cfg_path), "config", "show"])
    assert result.exit_code == 0
    assert "[defaults]" in result.output
    assert "[devices.cam1]" in result.output
    assert 'ip = "10.0.0.1"' in result.output


def test_config_validate_clean_exits_0(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[devices.cam1]\nip = "10.0.0.1"\n',
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(main, ["config", "validate", str(cfg_path)])
    assert result.exit_code == 0


def test_config_validate_bad_exits_6(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[bogus]\nx = 1\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(main, ["config", "validate", str(cfg_path)])
    assert result.exit_code == 6
    # Structured error envelope on stderr.
    parsed = json.loads(result.output.strip().splitlines()[-1])
    assert parsed["error"] == "config_error"
    assert parsed["exit_code"] == 6


def test_config_validate_no_path_no_default_exits_64(monkeypatch, tmp_path: Path) -> None:
    """No path passed AND no default config → usage error (exit 64)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(main, ["config", "validate"])
    assert result.exit_code == 64
