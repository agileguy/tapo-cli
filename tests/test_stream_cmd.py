"""Tests for ``tapo-cli stream`` (FR-12..12g, B6, S2).

Mock-only — no real network, no real ONVIF, no real exec'd children.

* RTSP URL construction with special-character passwords
* ONVIF profile fetch + --list-profiles
* Lens-by-protocol matrix (4 combos)
* --credentials-via-env redacts URL on stdout, sets env on child
* --exec ffmpeg ... substitutes URL last arg, no creds in argv when --creds-via-env
* No camera_account_file → exit 2
* Group target → exit 64
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner

from tapo_cli import auth_cache
from tapo_cli.cli import main
from tapo_cli.media import build_rtsp_url, redact_userinfo
from tapo_cli.verbs import stream_cmd as stream_v


@pytest.fixture(autouse=True)
def _redirect_home(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(auth_cache.ENV_CONFIG_DIR, str(tmp_path / "tapo-cli"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TAPO_CLI_CONFIG", raising=False)


def _camera_account_file(tmp_path: Path, *, user: str = "camuser", pw: str = "campw1234") -> Path:
    """Create a per-device camera-account file (mode 0600) and return its path."""
    cred = tmp_path / "cam-account.json"
    cred.write_text(
        json.dumps({"version": 1, "username": user, "password": pw})
    )
    cred.chmod(0o600)
    return cred


def _config_with_camera_account(tmp_path: Path, **cred_kwargs) -> Path:
    cam = _camera_account_file(tmp_path, **cred_kwargs)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[devices.cam1]\n'
        'ip = "10.0.0.42"\n'
        'mac = "AA:BB:CC:DD:EE:01"\n'
        'model = "C200"\n'
        f'camera_account_file = "{cam}"\n',
        encoding="utf-8",
    )
    return cfg


# ---------------------------------------------------------------------------
# URL construction (FR-12, S2)
# ---------------------------------------------------------------------------


def test_url_construction_simple_password() -> None:
    url = build_rtsp_url("10.0.0.42", "user", "pass", path="stream1")
    assert url == "rtsp://user:pass@10.0.0.42:554/stream1"


def test_url_construction_special_chars_quoted() -> None:
    """Reserved chars in passwords MUST be percent-encoded so they don't
    corrupt the userinfo segment of the RTSP URL.
    """
    url = build_rtsp_url("10.0.0.42", "user", "pa@ss/wo:rd!?#&", path="stream1")
    # @ → %40, : → %3A, / → %2F, ! → %21, ? → %3F, # → %23, & → %26
    assert "%40" in url
    assert "%3A" in url
    assert "%2F" in url
    assert "%21" in url
    assert "%3F" in url
    assert "%23" in url
    assert "%26" in url
    # Cleartext should NOT contain the reserved chars (post-encoding).
    userinfo = url.split("@")[0].split("//")[1]
    assert "@" not in userinfo  # the only @ is the one before host
    assert "?" not in userinfo
    assert "#" not in userinfo


def test_redact_userinfo_replaces_with_placeholders() -> None:
    full = "rtsp://camuser:secret@1.2.3.4:554/stream1"
    redacted = redact_userinfo(full)
    assert redacted == "rtsp://<user>:<pass>@1.2.3.4:554/stream1"


# ---------------------------------------------------------------------------
# Default emission: bare URL on stdout (FR-12)
# ---------------------------------------------------------------------------


def test_default_emits_bare_rtsp_url(tmp_path: Path) -> None:
    cfg = _config_with_camera_account(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["--config", str(cfg), "stream", "cam1"])
    assert result.exit_code == 0, result.output
    line = result.output.strip()
    assert line.startswith("rtsp://")
    assert "@10.0.0.42:554/stream1" in line


def test_default_emits_url_at_alias_prefix_form(tmp_path: Path) -> None:
    """``@cam1`` (alias prefix, NOT a group) is accepted same as ``cam1``."""
    cfg = _config_with_camera_account(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["--config", str(cfg), "stream", "@cam1"])
    assert result.exit_code == 0, result.output
    assert result.output.strip().startswith("rtsp://")


# ---------------------------------------------------------------------------
# Lens-by-quality truth table (B6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lens", "quality", "expected_path"),
    [
        ("wide", "hd", "stream1"),
        ("wide", "sd", "stream2"),
        ("telephoto", "hd", "stream6"),
        ("telephoto", "sd", "stream7"),
    ],
)
def test_lens_quality_matrix(
    tmp_path: Path, lens: str, quality: str, expected_path: str
) -> None:
    cfg = _config_with_camera_account(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(cfg), "stream", "cam1", "--lens", lens, "--quality", quality],
    )
    assert result.exit_code == 0, result.output
    assert f"/{expected_path}" in result.output


def test_protocol_override_bypasses_truth_table(tmp_path: Path) -> None:
    cfg = _config_with_camera_account(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(cfg), "stream", "cam1", "--protocol", "stream6"],
    )
    assert result.exit_code == 0
    assert "/stream6" in result.output


# ---------------------------------------------------------------------------
# ONVIF profiles (FR-12b.1, FR-12b.2)
# ---------------------------------------------------------------------------


def test_list_profiles_emits_json_array(tmp_path: Path) -> None:
    """``--list-profiles`` SHALL emit the GetProfiles result as JSON, exit 0."""
    cfg = _config_with_camera_account(tmp_path)
    fake_profiles = [
        {"name": "mainStream", "token": "tok1", "encoder": "H264", "resolution": "1920x1080"},
        {"name": "subStream", "token": "tok2", "encoder": "H264", "resolution": "640x360"},
    ]

    async def _fake_fetch(*args, **kwargs):
        return fake_profiles

    runner = CliRunner()
    with mock.patch.object(stream_v, "_fetch_onvif_profiles", side_effect=_fake_fetch):
        result = runner.invoke(
            main, ["--config", str(cfg), "stream", "cam1", "--list-profiles"]
        )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed == fake_profiles
    assert parsed[0]["name"] == "mainStream"


def test_list_profiles_onvif_unavailable_exits_5(tmp_path: Path) -> None:
    """ONVIF unreachable → exit 5 (unsupported_feature)."""
    cfg = _config_with_camera_account(tmp_path)

    async def _fake_fetch(*args, **kwargs):
        raise RuntimeError("ONVIF connect refused")

    runner = CliRunner()
    with mock.patch.object(stream_v, "_fetch_onvif_profiles", side_effect=_fake_fetch):
        result = runner.invoke(
            main, ["--jsonl", "--config", str(cfg), "stream", "cam1", "--list-profiles"]
        )
    assert result.exit_code == 5
    err = json.loads(result.output.strip().splitlines()[-1])
    assert err["error"] == "unsupported_feature"


def test_profile_name_resolves_via_onvif(tmp_path: Path) -> None:
    cfg = _config_with_camera_account(tmp_path)
    fake_profiles = [
        {"name": "mainStream", "token": "tok1", "encoder": "H264", "resolution": "1920x1080"},
        {"name": "subStream", "token": "tok2", "encoder": "H264", "resolution": "640x360"},
    ]

    async def _fake_fetch(*args, **kwargs):
        return fake_profiles

    runner = CliRunner()
    with mock.patch.object(stream_v, "_fetch_onvif_profiles", side_effect=_fake_fetch):
        result = runner.invoke(
            main, ["--config", str(cfg), "stream", "cam1", "--profile", "subStream"]
        )
    assert result.exit_code == 0
    # subStream → /stream2 per heuristic mapper
    assert "/stream2" in result.output


# ---------------------------------------------------------------------------
# --credentials-via-env redaction (FR-12f, S2)
# ---------------------------------------------------------------------------


def test_credentials_via_env_redacts_url_on_stdout(tmp_path: Path) -> None:
    """With --credentials-via-env, stdout MUST NOT contain the actual creds."""
    cfg = _config_with_camera_account(tmp_path, user="camuser", pw="campw1234")
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(cfg), "stream", "cam1", "--credentials-via-env"],
    )
    assert result.exit_code == 0, result.output
    assert "camuser" not in result.output
    assert "campw1234" not in result.output
    assert "<user>:<pass>" in result.output
    assert "10.0.0.42" in result.output


# ---------------------------------------------------------------------------
# --exec child substitution (FR-12g)
# ---------------------------------------------------------------------------


def test_exec_substitutes_url_into_placeholder(tmp_path: Path) -> None:
    """``--exec ffmpeg -i {} -c copy out.mp4`` substitutes the URL into ``{}``."""
    cfg = _config_with_camera_account(tmp_path)
    captured: dict[str, list[str]] = {}

    def _fake_execvp(prog: str, args: list[str]) -> None:
        captured["prog"] = prog
        captured["args"] = list(args)
        # execvp normally never returns; raise SystemExit so the runner gets
        # a clean exit code.
        raise SystemExit(0)

    runner = CliRunner()
    with mock.patch.object(stream_v.os, "execvp", side_effect=_fake_execvp):
        runner.invoke(
            main,
            [
                "--config", str(cfg),
                "stream", "cam1",
                "--exec",
                "ffmpeg", "-i", "{}", "-c", "copy", "out.mp4",
            ],
        )
    assert captured["prog"] == "ffmpeg"
    # {} replaced in place
    assert "{}" not in captured["args"]
    rtsp_idx = next(i for i, a in enumerate(captured["args"]) if a.startswith("rtsp://"))
    assert captured["args"][rtsp_idx].startswith("rtsp://")
    # Argv layout preserved
    assert captured["args"][0] == "ffmpeg"
    assert captured["args"][1] == "-i"
    # URL must contain the cleartext creds (without --credentials-via-env).
    assert "camuser" in captured["args"][rtsp_idx]


def test_exec_with_credentials_via_env_passes_no_creds_in_argv(tmp_path: Path) -> None:
    """With --credentials-via-env, the URL on argv is redacted; full URL flows
    through env vars only.
    """
    cfg = _config_with_camera_account(tmp_path, user="camuser", pw="campw1234")
    captured: dict[str, list[str]] = {}
    captured_env: dict[str, str] = {}

    def _fake_execvp(prog: str, args: list[str]) -> None:
        captured["prog"] = prog
        captured["args"] = list(args)
        # Capture env at exec time
        captured_env["RTSP_USER"] = stream_v.os.environ.get("RTSP_USER", "")
        captured_env["RTSP_PASS"] = stream_v.os.environ.get("RTSP_PASS", "")
        captured_env["RTSP_URL"] = stream_v.os.environ.get("RTSP_URL", "")
        raise SystemExit(0)

    runner = CliRunner()
    with mock.patch.object(stream_v.os, "execvp", side_effect=_fake_execvp):
        runner.invoke(
            main,
            [
                "--config", str(cfg),
                "stream", "cam1",
                "--credentials-via-env",
                "--exec",
                "ffmpeg", "-i", "{}", "-c", "copy", "out.mp4",
            ],
        )
    # Creds NOWHERE on argv.
    joined = " ".join(captured["args"])
    assert "camuser" not in joined
    assert "campw1234" not in joined
    # Redacted URL on argv.
    assert any("<user>:<pass>" in a for a in captured["args"])
    # Full creds via env.
    assert captured_env["RTSP_USER"] == "camuser"
    assert captured_env["RTSP_PASS"] == "campw1234"
    assert captured_env["RTSP_URL"].startswith("rtsp://camuser:campw1234@")


def test_exec_appends_url_when_no_placeholder(tmp_path: Path) -> None:
    cfg = _config_with_camera_account(tmp_path)
    captured: dict[str, list[str]] = {}

    def _fake_execvp(prog: str, args: list[str]) -> None:
        captured["args"] = list(args)
        raise SystemExit(0)

    runner = CliRunner()
    with mock.patch.object(stream_v.os, "execvp", side_effect=_fake_execvp):
        runner.invoke(
            main,
            [
                "--config", str(cfg),
                "stream", "cam1",
                "--exec",
                "ffplay",
            ],
        )
    # ffplay <URL>
    assert captured["args"][0] == "ffplay"
    assert captured["args"][-1].startswith("rtsp://")
    assert len(captured["args"]) == 2


# ---------------------------------------------------------------------------
# Camera-account-only path (FR-CRED-7)
# ---------------------------------------------------------------------------


def test_no_camera_account_file_exits_2(tmp_path: Path) -> None:
    """Stream against a device WITHOUT camera_account_file MUST exit 2."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[devices.cam1]\nip = "10.0.0.42"\n',  # no camera_account_file
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(main, ["--jsonl", "--config", str(cfg), "stream", "cam1"])
    assert result.exit_code == 2, result.output
    err = json.loads(result.output.strip().splitlines()[-1])
    assert err["error"] == "auth_failed"
    assert err["credential"] == "camera_account"
    assert "Tapo app" in err["hint"] or "Camera account" in err["hint"]


# ---------------------------------------------------------------------------
# Group rejection (FR-49 / FR-43c)
# ---------------------------------------------------------------------------


def test_group_target_rejected_exits_64(tmp_path: Path) -> None:
    cam = _camera_account_file(tmp_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[devices.cam1]\n'
        'ip = "10.0.0.42"\n'
        f'camera_account_file = "{cam}"\n\n'
        '[groups]\n'
        'indoor = ["cam1"]\n',
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(main, ["--jsonl", "--config", str(cfg), "stream", "@indoor"])
    assert result.exit_code == 64, result.output
    err = json.loads(result.output.strip().splitlines()[-1])
    assert err["error"] == "usage_error"
    assert "group" in err["message"].lower()


# ---------------------------------------------------------------------------
# Profile-resolver behavior (FR-12b)
# ---------------------------------------------------------------------------


def test_explicit_json_emits_record_with_resolver_field(tmp_path: Path) -> None:
    """``--json`` mode emits the structured Stream record including ``resolver``."""
    cfg = _config_with_camera_account(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["--json", "--config", str(cfg), "stream", "cam1"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["target"] == "cam1"
    assert payload["url"].startswith("rtsp://")
    assert payload["protocol"] == "rtsp"
    assert payload["lens"] == "wide"
    assert payload["quality"] == "hd"
    assert payload["resolver"] in ("onvif", "defaults")
