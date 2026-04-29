"""Tests for the TOML configuration loader (SRD §9)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tapo_cli.config import (
    Config,
    DeviceEntry,
    effective_toml,
    load_config,
    validate_config,
)
from tapo_cli.errors import ConfigError

# ---------------------------------------------------------------------------
# Defaults / built-ins
# ---------------------------------------------------------------------------


def test_defaults_when_no_config_file_present(monkeypatch, tmp_path: Path) -> None:
    """Missing default file → built-in Config, no error."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # tomllib will not see a default config because $HOME redirected.
    monkeypatch.delenv("TAPO_CLI_CONFIG", raising=False)
    cfg = load_config(None)
    assert cfg.source_path is None
    assert cfg.defaults.timeout_seconds == 5
    assert cfg.defaults.concurrency == 5
    assert cfg.credentials.file_path == "~/.config/kasa-cli/credentials"


def test_explicit_path_missing_exits_6(tmp_path: Path) -> None:
    missing = tmp_path / "nope.toml"
    with pytest.raises(ConfigError) as ei:
        load_config(missing)
    assert ei.value.exit_code == 6
    assert "config file not found" in ei.value.message


def test_env_var_path_missing_exits_6(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TAPO_CLI_CONFIG", str(tmp_path / "nope.toml"))
    with pytest.raises(ConfigError) as ei:
        load_config(None)
    assert ei.value.exit_code == 6


# ---------------------------------------------------------------------------
# Parsing / round-trip
# ---------------------------------------------------------------------------


def _write(p: Path, body: str) -> Path:
    p.write_text(body, encoding="utf-8")
    return p


def test_full_config_round_trip(tmp_path: Path) -> None:
    body = """
[defaults]
timeout_seconds = 7
concurrency = 3
output_format = "json"

[credentials]
file_path = "~/.config/tapo-cli/credentials"

[ffmpeg]
path = "/opt/homebrew/bin/ffmpeg"

[logging]
file = "~/.local/state/tapo-cli/log"

[devices.front-door]
ip = "192.168.1.42"
mac = "AA:BB:CC:DD:EE:01"
model = "D230"
camera_account_file = "~/.config/tapo-cli/cam-accounts/front-door.json"

[devices.backyard]
ip = "192.168.1.51"
mac = "AA:BB:CC:DD:EE:02"
model = "C320WS"

[groups]
perimeter = ["front-door", "backyard"]
"""
    p = _write(tmp_path / "c.toml", body)
    cfg = load_config(p)
    assert cfg.defaults.timeout_seconds == 7
    assert cfg.defaults.concurrency == 3
    assert cfg.defaults.output_format == "json"
    assert cfg.credentials.file_path == "~/.config/tapo-cli/credentials"
    assert cfg.ffmpeg.path == "/opt/homebrew/bin/ffmpeg"
    assert cfg.logging.file == "~/.local/state/tapo-cli/log"

    fd = cfg.devices["front-door"]
    assert fd.ip == "192.168.1.42"
    assert fd.mac == "AA:BB:CC:DD:EE:01"
    assert fd.model == "D230"
    assert fd.camera_account_file == "~/.config/tapo-cli/cam-accounts/front-door.json"
    assert cfg.groups["perimeter"] == ["front-door", "backyard"]

    # effective_toml round-trip is canonical.
    rendered = effective_toml(cfg)
    assert "[defaults]" in rendered
    assert 'output_format = "json"' in rendered
    assert "[devices.front-door]" in rendered
    assert "[devices.backyard]" in rendered


def test_unknown_top_level_table_exits_6(tmp_path: Path) -> None:
    p = _write(tmp_path / "c.toml", "[bogus]\nx = 1\n")
    with pytest.raises(ConfigError) as ei:
        load_config(p)
    assert ei.value.exit_code == 6
    assert "unknown top-level" in ei.value.message


def test_unknown_defaults_key_exits_6(tmp_path: Path) -> None:
    p = _write(tmp_path / "c.toml", '[defaults]\nbananas = 7\n')
    with pytest.raises(ConfigError) as ei:
        load_config(p)
    assert ei.value.exit_code == 6
    assert "unknown keys in [defaults]" in ei.value.message


def test_invalid_output_format_exits_6(tmp_path: Path) -> None:
    p = _write(tmp_path / "c.toml", '[defaults]\noutput_format = "yaml"\n')
    with pytest.raises(ConfigError) as ei:
        load_config(p)
    assert ei.value.exit_code == 6


def test_negative_timeout_exits_6(tmp_path: Path) -> None:
    p = _write(tmp_path / "c.toml", "[defaults]\ntimeout_seconds = -1\n")
    with pytest.raises(ConfigError):
        load_config(p)


def test_group_member_must_exist(tmp_path: Path) -> None:
    body = """
[devices.cam1]
ip = "192.168.1.10"

[groups]
mygroup = ["cam1", "ghost"]
"""
    p = _write(tmp_path / "c.toml", body)
    with pytest.raises(ConfigError) as ei:
        load_config(p)
    assert ei.value.exit_code == 6
    assert "ghost" in ei.value.message


def test_camera_account_file_field_round_trips(tmp_path: Path) -> None:
    body = """
[devices.cam1]
ip = "192.168.1.10"
camera_account_file = "/tmp/cred.json"
"""
    p = _write(tmp_path / "c.toml", body)
    cfg = load_config(p)
    assert cfg.devices["cam1"].camera_account_file == "/tmp/cred.json"


def test_validate_config_helper_raises_without_installing(tmp_path: Path) -> None:
    body = "[bogus]\nx = 1\n"
    p = _write(tmp_path / "c.toml", body)
    with pytest.raises(ConfigError):
        validate_config(p)


def test_malformed_toml_exits_6(tmp_path: Path) -> None:
    p = _write(tmp_path / "c.toml", "[defaults\nbroken = 1")
    with pytest.raises(ConfigError) as ei:
        load_config(p)
    assert ei.value.exit_code == 6


def test_devices_table_without_inner_alias_table_errors(tmp_path: Path) -> None:
    """``devices = "not a table"`` — top-level devices must be a table."""
    p = _write(tmp_path / "c.toml", 'devices = "x"\n')
    with pytest.raises(ConfigError):
        load_config(p)


def test_effective_toml_alphabetizes_devices() -> None:
    cfg = Config(
        devices={
            "zeta": DeviceEntry(alias="zeta", ip="10.0.0.3"),
            "alpha": DeviceEntry(alias="alpha", ip="10.0.0.1"),
        },
    )
    out = effective_toml(cfg)
    alpha_idx = out.index("[devices.alpha]")
    zeta_idx = out.index("[devices.zeta]")
    assert alpha_idx < zeta_idx


def test_unknown_device_key_exits_6(tmp_path: Path) -> None:
    body = """
[devices.cam1]
ip = "192.168.1.10"
randomstuff = "no"
"""
    p = _write(tmp_path / "c.toml", body)
    with pytest.raises(ConfigError) as ei:
        load_config(p)
    assert ei.value.exit_code == 6
    assert "randomstuff" in str(ei.value.extra) or "randomstuff" in ei.value.message
