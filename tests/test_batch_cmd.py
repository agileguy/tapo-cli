"""Tests for ``tapo-cli batch`` (Phase 3, FR-44..45c, B10)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from tapo_cli import auth_cache
from tapo_cli.cli import main


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(auth_cache.ENV_CONFIG_DIR, str(tmp_path / "tapo-cli"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TAPO_CLI_CONFIG", raising=False)


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


# ---------------------------------------------------------------------------
# Empty / parse-only paths
# ---------------------------------------------------------------------------


def test_batch_requires_stdin_or_file_exits_64(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--config", str(_cfg(tmp_path)), "batch"])
    assert result.exit_code == 64, result.output


def test_batch_empty_stdin_exits_zero_with_no_stdout(tmp_path: Path) -> None:
    """FR-45b: empty input → exit 0 silently."""
    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "batch", "--stdin"], input=""
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == ""


def test_batch_blank_lines_and_comments_skipped(tmp_path: Path) -> None:
    runner = CliRunner()
    payload = "\n\n# a comment\n   \n# another\n"
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "batch", "--stdin"],
        input=payload,
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == ""


def test_batch_file_not_found_exits_64(tmp_path: Path) -> None:
    runner = CliRunner()
    bogus = tmp_path / "nope.txt"
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "batch", "--file", str(bogus)]
    )
    assert result.exit_code == 64, result.output


def test_batch_mutex_stdin_and_file_exits_64(tmp_path: Path) -> None:
    payload = tmp_path / "p.txt"
    payload.write_text("# hi\n")
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "batch",
            "--stdin",
            "--file",
            str(payload),
        ],
    )
    assert result.exit_code == 64, result.output


# ---------------------------------------------------------------------------
# JSONL per-line shape (B10)
# ---------------------------------------------------------------------------


def test_batch_groups_list_emits_per_line_jsonl(tmp_path: Path) -> None:
    """``groups list`` is a deterministic, no-network sub-command — perfect
    for asserting the per-line JSONL shape (B10) without mocking devices.
    """
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "batch", "--stdin"],
        input="groups list\n",
    )
    assert result.exit_code == 0, result.output
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    # B10 keys.
    for k in ("command", "target", "status", "exit_code"):
        assert k in parsed, parsed
    assert parsed["status"] == "ok"
    assert parsed["exit_code"] == 0
    assert "result" in parsed
    # The 'groups list' result is a list (one entry per group).
    assert isinstance(parsed["result"], list)


def test_batch_unknown_target_for_alarm_status_emits_error_shape(
    tmp_path: Path,
) -> None:
    """A sub-command that fails surfaces the structured error in B10's
    ``error`` slot. ``alarm @nope status`` is unknown-alias → exit 4."""
    runner = CliRunner()
    payload = "alarm @nonexistent status\n"
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "batch", "--stdin"],
        input=payload,
    )
    # Single failed line → exit code mirrors the sub-op's exit code (B9).
    assert result.exit_code != 0, result.output
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["status"] == "error"
    assert parsed["exit_code"] != 0
    assert "error" in parsed
    assert "code" in parsed["error"]
    assert "message" in parsed["error"]


# ---------------------------------------------------------------------------
# Exit code semantics (FR-43a / FR-45a / B9)
# ---------------------------------------------------------------------------


def test_batch_all_pass_exits_zero(tmp_path: Path) -> None:
    runner = CliRunner()
    payload = "groups list\nconfig validate\n"
    # config validate with no path AND no default config will exit 64;
    # Instead use config show which always succeeds.
    payload = "groups list\nconfig show\n"
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "batch", "--stdin"],
        input=payload,
    )
    assert result.exit_code == 0, result.output
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 2
    for line in lines:
        parsed = json.loads(line)
        assert parsed["status"] == "ok", parsed


def test_batch_partial_failure_exits_7(tmp_path: Path) -> None:
    runner = CliRunner()
    payload = "groups list\nalarm @bogus status\n"
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "batch", "--stdin"],
        input=payload,
    )
    assert result.exit_code == 7, result.output
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 2
    statuses = [json.loads(line)["status"] for line in lines]
    assert "ok" in statuses
    assert "error" in statuses


def test_batch_all_fail_exits_first_failure_code(tmp_path: Path) -> None:
    """B9 deterministic: all-fail → exit code of the FIRST sub-op."""
    runner = CliRunner()
    # First line: shlex parse error (unbalanced quote) → exit 64.
    # Second line: unknown alias → exit 4.
    # All-fail → exit 64 (the first sub-op's code).
    payload = '"""\nalarm @bogus status\n'
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "batch", "--stdin"],
        input=payload,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert all(p["status"] == "error" for p in parsed)
    # B9: deterministic — first sub-op's exit code.
    assert result.exit_code == parsed[0]["exit_code"]


def test_batch_preserves_input_order_in_output(tmp_path: Path) -> None:
    runner = CliRunner()
    payload = "groups list\ngroups list\ngroups list\n"
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "batch", "--stdin"],
        input=payload,
    )
    assert result.exit_code == 0, result.output
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 3
    for line in lines:
        parsed = json.loads(line)
        assert parsed["command"] == "groups list"


def test_batch_file_input_works(tmp_path: Path) -> None:
    payload_path = tmp_path / "batch.txt"
    payload_path.write_text("groups list\n# this is a comment\n\ngroups list\n")
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "batch",
            "--file",
            str(payload_path),
        ],
    )
    assert result.exit_code == 0, result.output
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 2
