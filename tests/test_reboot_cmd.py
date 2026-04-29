"""Tests for ``tapo-cli reboot <target>`` (FR-38, S13, Phase 1d).

The confirmation matrix:

==========  ==========  ============  =================
tty?        --yes?      --quiet?      Result
==========  ==========  ============  =================
yes         no          no            prompt; y/n decides
yes         yes         no            no prompt; reboot
no          no          no            exit 64
no          yes         no            no prompt; reboot
no          no          yes           --quiet implies --yes; reboot
==========  ==========  ============  =================
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
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[devices.office]\nip = "192.168.1.11"\nmac = "AA:BB:CC:DD:EE:02"\n',
        encoding="utf-8",
    )
    return cfg_path


class _FakeTapo:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    def reboot(self, delay: object = None) -> dict[str, int]:
        self.calls.append(("reboot", delay))
        return {"error_code": 0}


def _patch_connect(monkeypatch, tapo: _FakeTapo) -> None:
    async def _fake_connect(cfg, target, *, credential_source=None, timeout=5.0):
        return TapoConnection(
            tapo=tapo,
            target=TapoTarget(alias="office", ip="192.168.1.11"),
            credential_family="camera_account",
        )

    monkeypatch.setattr("tapo_cli.wrapper.connect", _fake_connect)


# ---------------------------------------------------------------------------
# --yes bypass (the easy path)
# ---------------------------------------------------------------------------


def test_reboot_with_yes_proceeds(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "--json", "reboot", "office", "--yes"]
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed == {"target": "office", "status": "reboot-issued"}
    assert any(c[0] == "reboot" for c in fake.calls)


def test_reboot_short_y_flag(tmp_path: Path, monkeypatch) -> None:
    """``-y`` is the short alias for ``--yes``."""
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "--json", "reboot", "office", "-y"]
    )
    assert result.exit_code == 0, result.output
    assert any(c[0] == "reboot" for c in fake.calls)


# ---------------------------------------------------------------------------
# --quiet implies --yes (FR-38)
# ---------------------------------------------------------------------------


def test_reboot_quiet_implies_yes(tmp_path: Path, monkeypatch) -> None:
    """--quiet alone (no --yes) should still proceed without prompting."""
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "--quiet", "reboot", "office"]
    )
    assert result.exit_code == 0, result.output
    # --quiet → no stdout output.
    assert result.stdout.strip() == ""
    assert any(c[0] == "reboot" for c in fake.calls)


# ---------------------------------------------------------------------------
# Non-tty without --yes → exit 64 (S13)
# ---------------------------------------------------------------------------


def test_reboot_non_tty_without_yes_exits_64(tmp_path: Path, monkeypatch) -> None:
    """CliRunner runs in a non-tty environment by default — exit 64 expected."""
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "--json", "reboot", "office"]
    )
    assert result.exit_code == 64, result.output
    # The reboot RPC MUST NOT have been called.
    assert all(c[0] != "reboot" for c in fake.calls)


# ---------------------------------------------------------------------------
# tty prompt path (yes / no answers)
# ---------------------------------------------------------------------------


def _force_tty(monkeypatch) -> None:
    """Force ``_confirm_or_fail`` to take the interactive-tty branch.

    CliRunner replaces ``sys.stdin``/``sys.stderr`` with non-tty buffers,
    and Click does that AFTER fixture monkeypatches run. ``reboot_cmd``
    routes its tty check through ``_is_interactive_tty`` for exactly this
    reason — patch that one function and the rest falls into place.
    """
    monkeypatch.setattr(
        "tapo_cli.verbs.reboot_cmd._is_interactive_tty", lambda: True
    )


def test_reboot_tty_prompt_accepts_yes(tmp_path: Path, monkeypatch) -> None:
    """Force tty detection ON, feed ``y`` on stdin → reboot proceeds."""
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    _force_tty(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--json", "reboot", "office"],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert any(c[0] == "reboot" for c in fake.calls)


def test_reboot_tty_prompt_rejects_with_n(tmp_path: Path, monkeypatch) -> None:
    """Force tty detection ON, feed ``N`` on stdin → no reboot, exit 0."""
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    _force_tty(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--json", "reboot", "office"],
        input="N\n",
    )
    # Operator declined; exit 0, no action taken.
    assert result.exit_code == 0, result.output
    assert all(c[0] != "reboot" for c in fake.calls)


def test_reboot_tty_prompt_default_is_no(tmp_path: Path, monkeypatch) -> None:
    """Bare enter (empty input) defaults to N → no reboot."""
    fake = _FakeTapo()
    _patch_connect(monkeypatch, fake)
    _force_tty(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--json", "reboot", "office"],
        input="\n",
    )
    assert result.exit_code == 0, result.output
    assert all(c[0] != "reboot" for c in fake.calls)
