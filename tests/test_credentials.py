"""Tests for the credential resolver (SRD §6, FR-CRED-1..15)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from tapo_cli import credentials
from tapo_cli.config import Config, CredentialsConfig, DeviceEntry
from tapo_cli.errors import AuthError, ConfigError


def _write_v1(p: Path, username: str, password: str, *, mode: int = 0o600) -> Path:
    p.write_text(
        json.dumps({"version": 1, "username": username, "password": password}),
        encoding="utf-8",
    )
    os.chmod(p, mode)
    return p


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Clear credentials' once-per-process latches and env vars."""
    credentials._reset_deprecation_state_for_tests()
    monkeypatch.delenv(credentials.ENV_USERNAME, raising=False)
    monkeypatch.delenv(credentials.ENV_PASSWORD, raising=False)


# ---------------------------------------------------------------------------
# Cloud-account chain (FR-CRED-1..3, FR-CRED-3.1)
# ---------------------------------------------------------------------------


def test_cloud_default_resolves_via_kasa_shared_path(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    kasa_path = home / ".config" / "kasa-cli" / "credentials"
    kasa_path.parent.mkdir(parents=True)
    _write_v1(kasa_path, "alice@example.com", "secret123")

    cfg = Config(credentials=CredentialsConfig(file_path="~/.config/kasa-cli/credentials"))
    cred = credentials.resolve_control_plane(cfg)
    assert cred is not None
    assert cred.family == "cloud_account"
    assert cred.username == "alice@example.com"
    assert "kasa-cli" in cred.source


def test_tapo_only_override_wins_over_kasa_shared(monkeypatch, tmp_path: Path) -> None:
    """FR-CRED-3.1: ~/.config/tapo-cli/credentials wins when both exist."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    kasa_path = home / ".config" / "kasa-cli" / "credentials"
    kasa_path.parent.mkdir(parents=True)
    _write_v1(kasa_path, "kasa@example.com", "kasapass")

    tapo_path = home / ".config" / "tapo-cli" / "credentials"
    tapo_path.parent.mkdir(parents=True)
    _write_v1(tapo_path, "tapo@example.com", "tapopass")

    cfg = Config()
    cred = credentials.resolve_control_plane(cfg)
    assert cred is not None
    assert cred.username == "tapo@example.com"
    assert "tapo-cli" in cred.source


def test_chmod_violation_exits_2(monkeypatch, tmp_path: Path) -> None:
    """FR-CRED-2: file with mode > 0600 → AuthError, exit 2."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    p = home / ".config" / "kasa-cli" / "credentials"
    p.parent.mkdir(parents=True)
    _write_v1(p, "u", "secret123", mode=0o644)

    cfg = Config()
    with pytest.raises(AuthError) as ei:
        credentials.resolve_control_plane(cfg)
    assert ei.value.exit_code == 2
    assert "0600" in ei.value.message or "0o600" in ei.value.message


def test_symlink_credential_file_refused(monkeypatch, tmp_path: Path) -> None:
    """R5: refuse symlinks for credential files."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    real = home / "real"
    _write_v1(real, "u", "secret123")
    link = home / ".config" / "kasa-cli" / "credentials"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)

    cfg = Config()
    with pytest.raises(AuthError, match="symlink"):
        credentials.resolve_control_plane(cfg)


def test_env_vars_resolve_when_both_set(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(credentials.ENV_USERNAME, "envuser@example.com")
    monkeypatch.setenv(credentials.ENV_PASSWORD, "envpass1")

    cfg = Config()
    cred = credentials.resolve_control_plane(cfg)
    assert cred is not None
    assert cred.source == "env"
    assert cred.username == "envuser@example.com"


def test_partial_env_falls_through_with_warn(monkeypatch, tmp_path: Path, caplog) -> None:
    """§6.2 note: partial env-var set must fall through and WARN once."""
    import logging

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(credentials.ENV_USERNAME, "only-user")
    monkeypatch.delenv(credentials.ENV_PASSWORD, raising=False)

    cfg = Config()
    with caplog.at_level(logging.WARNING, logger="tapo_cli"):
        cred = credentials.resolve_control_plane(cfg)
    assert cred is None  # nothing else configured
    warns = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("partial credential env-vars" in r.getMessage() for r in warns)


def test_credential_source_none_skips_everything(monkeypatch, tmp_path: Path) -> None:
    """FR-CRED-15: --credential-source none → return None even if a file exists."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    p = home / ".config" / "kasa-cli" / "credentials"
    p.parent.mkdir(parents=True)
    _write_v1(p, "u", "secret123")
    monkeypatch.setenv(credentials.ENV_USERNAME, "u")
    monkeypatch.setenv(credentials.ENV_PASSWORD, "p")

    cfg = Config()
    assert credentials.resolve_control_plane(cfg, source="none") is None


def test_credential_source_env_skips_files(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    p = home / ".config" / "kasa-cli" / "credentials"
    p.parent.mkdir(parents=True)
    _write_v1(p, "fileuser", "filepass1")
    monkeypatch.setenv(credentials.ENV_USERNAME, "envuser@example.com")
    monkeypatch.setenv(credentials.ENV_PASSWORD, "envpass1")

    cfg = Config()
    cred = credentials.resolve_control_plane(cfg, source="env")
    assert cred is not None
    assert cred.username == "envuser@example.com"


def test_credential_source_file_skips_env(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    p = home / ".config" / "kasa-cli" / "credentials"
    p.parent.mkdir(parents=True)
    _write_v1(p, "fileuser", "filepass1")
    monkeypatch.setenv(credentials.ENV_USERNAME, "envuser@example.com")
    monkeypatch.setenv(credentials.ENV_PASSWORD, "envpass1")

    cfg = Config()
    cred = credentials.resolve_control_plane(cfg, source="file")
    assert cred is not None
    assert cred.username == "fileuser"


# ---------------------------------------------------------------------------
# Camera-account format (FR-CRED-4..7)
# ---------------------------------------------------------------------------


def test_camera_account_resolves_for_alias(tmp_path: Path) -> None:
    p = tmp_path / "cam.json"
    _write_v1(p, "useruser", "passpass")
    cfg = Config(
        devices={
            "front-door": DeviceEntry(
                alias="front-door",
                camera_account_file=str(p),
            )
        }
    )
    cred = credentials.resolve_camera_account(cfg, alias="front-door")
    assert cred.family == "camera_account"
    assert cred.username == "useruser"


def test_camera_account_missing_alias_exits_2(tmp_path: Path) -> None:
    cfg = Config(devices={})
    with pytest.raises(AuthError) as ei:
        credentials.resolve_camera_account(cfg, alias="missing-cam")
    assert ei.value.exit_code == 2
    assert ei.value.credential == "camera_account"


def test_camera_account_username_too_short_exits_6(tmp_path: Path) -> None:
    p = tmp_path / "cam.json"
    _write_v1(p, "abc", "longenoughpw")  # username < 6
    cfg = Config(
        devices={
            "cam": DeviceEntry(alias="cam", camera_account_file=str(p)),
        }
    )
    with pytest.raises(ConfigError) as ei:
        credentials.resolve_camera_account(cfg, alias="cam")
    assert ei.value.exit_code == 6
    assert "username" in ei.value.message


def test_camera_account_password_too_long_exits_6(tmp_path: Path) -> None:
    p = tmp_path / "cam.json"
    _write_v1(p, "useruser", "x" * 33)  # password > 32
    cfg = Config(
        devices={
            "cam": DeviceEntry(alias="cam", camera_account_file=str(p)),
        }
    )
    with pytest.raises(ConfigError) as ei:
        credentials.resolve_camera_account(cfg, alias="cam")
    assert ei.value.exit_code == 6
    assert "password" in ei.value.message


def test_camera_account_first_in_control_plane(tmp_path: Path) -> None:
    """FR-CRED-8: camera-account is consulted first when alias has one."""
    cam_p = tmp_path / "cam.json"
    _write_v1(cam_p, "camuser", "campass1")
    cloud_p = tmp_path / "cloud.json"
    _write_v1(cloud_p, "cloud@example.com", "cloudpass")

    cfg = Config(
        credentials=CredentialsConfig(file_path=str(cloud_p)),
        devices={
            "cam": DeviceEntry(
                alias="cam",
                camera_account_file=str(cam_p),
            )
        },
    )
    cred = credentials.resolve_control_plane(cfg, alias="cam")
    assert cred is not None
    assert cred.family == "camera_account"
    assert cred.username == "camuser"


# ---------------------------------------------------------------------------
# Schema invariants (unknown keys, missing version, bad JSON)
# ---------------------------------------------------------------------------


def test_unknown_keys_exit_6(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    p.write_text(
        json.dumps({"version": 1, "username": "u", "password": "p", "garbage": 1}),
        encoding="utf-8",
    )
    os.chmod(p, 0o600)
    cfg = Config(credentials=CredentialsConfig(file_path=str(p)))
    with pytest.raises(ConfigError) as ei:
        credentials.resolve_control_plane(cfg)
    assert ei.value.exit_code == 6
    assert "garbage" in str(ei.value.extra)


def test_invalid_json_exits_6(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    p.write_text("{not valid", encoding="utf-8")
    os.chmod(p, 0o600)
    cfg = Config(credentials=CredentialsConfig(file_path=str(p)))
    with pytest.raises(ConfigError):
        credentials.resolve_control_plane(cfg)


def test_missing_version_warns_once_per_path(monkeypatch, tmp_path: Path, caplog) -> None:
    """FR-CRED-1: missing version → assume v1 + one stderr WARN per path."""
    import logging

    p = tmp_path / "x.json"
    p.write_text(json.dumps({"username": "u", "password": "secret123"}), encoding="utf-8")
    os.chmod(p, 0o600)
    cfg = Config(credentials=CredentialsConfig(file_path=str(p)))

    with caplog.at_level(logging.WARNING, logger="tapo_cli"):
        cred1 = credentials.resolve_control_plane(cfg)
        cred2 = credentials.resolve_control_plane(cfg)
    assert cred1 is not None and cred2 is not None
    warn_messages = [
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    ]
    # One WARN total for that path, even across two calls.
    matching = [m for m in warn_messages if "lacks a 'version' field" in m]
    assert len(matching) == 1


def test_unsupported_version_exits_6(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    p.write_text(
        json.dumps({"version": 99, "username": "u", "password": "p"}), encoding="utf-8"
    )
    os.chmod(p, 0o600)
    cfg = Config(credentials=CredentialsConfig(file_path=str(p)))
    with pytest.raises(ConfigError) as ei:
        credentials.resolve_control_plane(cfg)
    assert ei.value.exit_code == 6


# ---------------------------------------------------------------------------
# tapo-cli auth migrate refuses kasa-cli path (FR-CRED-3.1, FR-CRED-15a)
# ---------------------------------------------------------------------------


def test_auth_migrate_refuses_kasa_cli_path(monkeypatch, tmp_path: Path) -> None:
    """FR-CRED-3.1 invariant: migrate SHALL only act on tapo-only path."""
    from click.testing import CliRunner

    from tapo_cli.cli import main

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    kasa_path = home / ".config" / "kasa-cli" / "credentials"
    kasa_path.parent.mkdir(parents=True)
    _write_v1(kasa_path, "kasa@example.com", "kasapass")

    tapo_dir = home / ".config" / "tapo-cli"
    tapo_dir.mkdir(parents=True)
    tapo_link = tapo_dir / "credentials"
    tapo_link.symlink_to(kasa_path)

    runner = CliRunner()
    result = runner.invoke(main, ["auth", "migrate"])
    assert result.exit_code == 6, result.output
    # Untouched: kasa file's contents stay.
    payload = json.loads(kasa_path.read_text())
    assert payload["username"] == "kasa@example.com"


def test_auth_migrate_no_op_when_tapo_file_absent(monkeypatch, tmp_path: Path) -> None:
    from click.testing import CliRunner

    from tapo_cli.cli import main

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    runner = CliRunner()
    result = runner.invoke(main, ["auth", "migrate"])
    assert result.exit_code == 0
    assert "nothing to migrate" in result.output


def test_auth_migrate_stamps_version_field(monkeypatch, tmp_path: Path) -> None:
    """FR-CRED-15a: migrate adds version=1 to a v0 (no version) tapo-only file."""
    from click.testing import CliRunner

    from tapo_cli.cli import main

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    tapo_path = home / ".config" / "tapo-cli" / "credentials"
    tapo_path.parent.mkdir(parents=True)
    tapo_path.write_text(
        json.dumps({"username": "alice@example.com", "password": "secret123"}),
        encoding="utf-8",
    )
    os.chmod(tapo_path, 0o600)

    runner = CliRunner()
    result = runner.invoke(main, ["auth", "migrate"])
    assert result.exit_code == 0, result.output

    payload = json.loads(tapo_path.read_text())
    assert payload["version"] == 1
    assert payload["username"] == "alice@example.com"
    # Mode preserved.
    mode = stat.S_IMODE(tapo_path.stat().st_mode)
    assert mode == 0o600
