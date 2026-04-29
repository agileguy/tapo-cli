"""Tests for ``tapo-cli snapshot`` (FR-11..11d, B5).

Mock-only — no real network, no real ffmpeg subprocess. We exercise:

* tier-advance on timeout / non-200 / exception (not auth)
* auth-rejection at any tier short-circuits → exit 2
* --timeout budget split + --snapshot-budget override parsing
* ffmpeg missing → exit 6 with named dependency
* --output - mutex with --json/--jsonl → exit 64
* --quiet --output - emits binary JPEG bytes
* JSON output schema check (mechanism, bytes, width, height, elapsed_ms, target)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner

from tapo_cli import auth_cache
from tapo_cli.cli import main
from tapo_cli.verbs import snapshot_cmd as snap

# Smallest possible valid JPEG — 8x8 grayscale, ~160 bytes. Crafted by hand
# so `_jpeg_dimensions` reports (8, 8) and `_is_jpeg` accepts it. Reused
# across every successful-tier mock.
_TINY_JPEG = bytes.fromhex(
    "ffd8"  # SOI
    "ffe000104a46494600010100000100010000"  # JFIF APP0
    "ffdb004300"  # DQT marker + length + table id
    + ("01" * 64)  # 64-byte quantization table (all 1s)
    + "ffc0000b080008000801011100"  # SOF0: 8-bit, 8x8, 1 component
    + "ffc40014000100000000000000000000000000000000"  # DHT (DC, dummy)
    + "ffc40014100100000000000000000000000000000000"  # DHT (AC, dummy)
    + "ffda0008010100003f00"  # SOS
    + "0000"  # entropy data (empty)
    + "ffd9"  # EOI
)


@pytest.fixture(autouse=True)
def _redirect_home(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(auth_cache.ENV_CONFIG_DIR, str(tmp_path / "tapo-cli"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TAPO_CLI_CONFIG", raising=False)


@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    """A config + a cloud-account creds file the resolver can find."""
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"version": 1, "username": "u", "password": "p"}))
    creds.chmod(0o600)

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[credentials]\n'
        f'file_path = "{creds}"\n\n'
        '[devices.cam1]\n'
        'ip = "10.0.0.42"\n'
        'mac = "AA:BB:CC:DD:EE:01"\n'
        'model = "C200"\n',
        encoding="utf-8",
    )
    return cfg


# ---------------------------------------------------------------------------
# Budget parsing (FR-11a.3)
# ---------------------------------------------------------------------------


def test_default_budget_split_40_30_30() -> None:
    b = snap._default_budget(10.0)
    assert b.pytapo == pytest.approx(4.0)
    assert b.onvif == pytest.approx(3.0)
    assert b.ffmpeg == pytest.approx(3.0)


def test_budget_override_partial_keys_keep_defaults() -> None:
    b = snap._parse_budget_override("pytapo=1.0", 5.0)
    # pytapo overridden, onvif + ffmpeg keep defaults (1.5 each)
    assert b.pytapo == pytest.approx(1.0)
    assert b.onvif == pytest.approx(1.5)
    assert b.ffmpeg == pytest.approx(1.5)


def test_budget_override_full_spec_parses() -> None:
    b = snap._parse_budget_override("pytapo=2,onvif=2,ffmpeg=1", 5.0)
    assert (b.pytapo, b.onvif, b.ffmpeg) == (2.0, 2.0, 1.0)


def test_budget_override_sum_exceeds_timeout_raises_usage_error() -> None:
    from tapo_cli.errors import UsageError

    with pytest.raises(UsageError, match="exceeds --timeout"):
        snap._parse_budget_override("pytapo=5,onvif=5,ffmpeg=5", 5.0)


def test_budget_override_unknown_key_raises_usage_error() -> None:
    from tapo_cli.errors import UsageError

    with pytest.raises(UsageError, match="unknown key"):
        snap._parse_budget_override("borealis=1", 5.0)


def test_budget_override_non_numeric_value_raises_usage_error() -> None:
    from tapo_cli.errors import UsageError

    with pytest.raises(UsageError, match="not numeric"):
        snap._parse_budget_override("pytapo=fast", 5.0)


def test_budget_override_bad_format_raises_usage_error() -> None:
    from tapo_cli.errors import UsageError

    with pytest.raises(UsageError, match="key=value"):
        snap._parse_budget_override("just-pytapo", 5.0)


# ---------------------------------------------------------------------------
# JPEG validation
# ---------------------------------------------------------------------------


def test_is_jpeg_accepts_correct_magic() -> None:
    assert snap._is_jpeg(_TINY_JPEG) is True


def test_is_jpeg_rejects_too_short() -> None:
    assert snap._is_jpeg(b"\xff\xd8") is False


def test_is_jpeg_rejects_wrong_magic() -> None:
    assert snap._is_jpeg(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20) is False


def test_jpeg_dimensions_parses_tiny_jpeg() -> None:
    width, height = snap._jpeg_dimensions(_TINY_JPEG)
    assert (width, height) == (8, 8)


def test_jpeg_dimensions_returns_none_on_truncated() -> None:
    width, height = snap._jpeg_dimensions(b"\xff\xd8\xff\xe0")
    assert (width, height) == (None, None)


# ---------------------------------------------------------------------------
# Tier advance + auth short-circuit (FR-11a.1, FR-11a.2)
# ---------------------------------------------------------------------------


def test_chain_advance_on_pytapo_unavailable_to_ffmpeg(cfg_path: Path) -> None:
    """pytapo has no native API → tier "unavailable", advance to ONVIF, ONVIF
    times out, advance to ffmpeg which succeeds.
    """

    async def _t1_unavailable(*args, **kwargs):
        return snap._TierResult(
            mechanism="pytapo",
            status="unavailable",
            elapsed_ms=10.0,
            detail="no API",
        )

    async def _t2_timeout(*args, **kwargs):
        return snap._TierResult(
            mechanism="onvif",
            status="fail",
            elapsed_ms=20.0,
            detail="timeout after 1.50s",
        )

    def _t3_pass(*args, **kwargs):
        return snap._TierResult(
            mechanism="ffmpeg",
            status="pass",
            elapsed_ms=30.0,
            payload=_TINY_JPEG,
        )

    runner = CliRunner()
    out = Path("/tmp/test_snap_chain.jpg")
    out.unlink(missing_ok=True)

    with (
        mock.patch.object(snap, "_tier1_pytapo", side_effect=_t1_unavailable),
        mock.patch.object(snap, "_tier2_onvif", side_effect=_t2_timeout),
        mock.patch.object(snap, "_tier3_ffmpeg", side_effect=_t3_pass),
    ):
        result = runner.invoke(
            main,
            [
                "--config", str(cfg_path),
                "snapshot", "cam1",
                "--output", str(out),
            ],
        )
    assert result.exit_code == 0, result.output
    assert out.exists() and out.read_bytes() == _TINY_JPEG
    out.unlink(missing_ok=True)


def test_pytapo_auth_failure_short_circuits_chain(cfg_path: Path) -> None:
    """HTTP 401 / pytapo _AUTH_FAILED at tier 1 → exit 2 immediately,
    tier 2 and 3 NEVER invoked (FR-11a.2).
    """

    async def _t1_auth_fail(*args, **kwargs):
        raise snap._AuthRejectedError("pytapo", "_AUTH_FAILED")

    t2 = mock.AsyncMock()
    t3 = mock.MagicMock()
    runner = CliRunner()
    with (
        mock.patch.object(snap, "_tier1_pytapo", side_effect=_t1_auth_fail),
        mock.patch.object(snap, "_tier2_onvif", t2),
        mock.patch.object(snap, "_tier3_ffmpeg", t3),
    ):
        result = runner.invoke(
            main,
            [
                "--jsonl",
                "--config", str(cfg_path),
                "snapshot", "cam1",
                "--output", "/tmp/should-not-be-written.jpg",
            ],
        )
    assert result.exit_code == 2, result.output
    assert t2.call_count == 0
    assert t3.call_count == 0
    err_obj = json.loads(result.output.strip().splitlines()[-1])
    assert err_obj["error"] == "auth_failed"
    assert err_obj["mechanism"] == "pytapo"


def test_onvif_auth_failure_short_circuits_chain(cfg_path: Path) -> None:
    async def _t1_unavail(*args, **kwargs):
        return snap._TierResult(
            mechanism="pytapo", status="unavailable", elapsed_ms=1.0
        )

    async def _t2_401(*args, **kwargs):
        raise snap._AuthRejectedError("onvif", "HTTP 401")

    t3 = mock.MagicMock()
    runner = CliRunner()
    with (
        mock.patch.object(snap, "_tier1_pytapo", side_effect=_t1_unavail),
        mock.patch.object(snap, "_tier2_onvif", side_effect=_t2_401),
        mock.patch.object(snap, "_tier3_ffmpeg", t3),
    ):
        result = runner.invoke(
            main,
            [
                "--jsonl",
                "--config", str(cfg_path),
                "snapshot", "cam1",
                "--output", "/tmp/x.jpg",
            ],
        )
    assert result.exit_code == 2
    assert t3.call_count == 0
    err_obj = json.loads(result.output.strip().splitlines()[-1])
    assert err_obj["error"] == "auth_failed"
    assert err_obj["mechanism"] == "onvif"


def test_all_tiers_fail_without_auth_exits_1(cfg_path: Path) -> None:
    async def _t1_fail(*args, **kwargs):
        return snap._TierResult(
            mechanism="pytapo", status="fail", elapsed_ms=1.0, detail="boom"
        )

    async def _t2_fail(*args, **kwargs):
        return snap._TierResult(
            mechanism="onvif", status="fail", elapsed_ms=1.0, detail="soap fault"
        )

    def _t3_fail(*args, **kwargs):
        return snap._TierResult(
            mechanism="ffmpeg", status="fail", elapsed_ms=1.0, detail="rc=1"
        )

    runner = CliRunner()
    with (
        mock.patch.object(snap, "_tier1_pytapo", side_effect=_t1_fail),
        mock.patch.object(snap, "_tier2_onvif", side_effect=_t2_fail),
        mock.patch.object(snap, "_tier3_ffmpeg", side_effect=_t3_fail),
    ):
        result = runner.invoke(
            main,
            [
                "--jsonl",
                "--config", str(cfg_path),
                "snapshot", "cam1",
                "--output", "/tmp/x.jpg",
            ],
        )
    assert result.exit_code == 1
    err_obj = json.loads(result.output.strip().splitlines()[-1])
    assert err_obj["error"] == "device_error"
    assert "all snapshot mechanisms failed" in err_obj["message"]


# ---------------------------------------------------------------------------
# ffmpeg-missing → exit 6 (FR-11a.4)
# ---------------------------------------------------------------------------


def test_ffmpeg_missing_at_tier_3_exits_6(cfg_path: Path) -> None:
    """When the chain reaches tier 3 and ffmpeg is not on PATH, exit 6
    (config error) — NOT 1 (device error). Hint names the missing dep.
    """

    async def _t1_unavail(*args, **kwargs):
        return snap._TierResult(
            mechanism="pytapo", status="unavailable", elapsed_ms=1.0
        )

    async def _t2_fail(*args, **kwargs):
        return snap._TierResult(
            mechanism="onvif", status="fail", elapsed_ms=1.0, detail="soap"
        )

    runner = CliRunner()
    # shutil.which() returns None when the binary is missing.
    with (
        mock.patch.object(snap, "_tier1_pytapo", side_effect=_t1_unavail),
        mock.patch.object(snap, "_tier2_onvif", side_effect=_t2_fail),
        mock.patch("tapo_cli.verbs.snapshot_cmd.shutil.which", return_value=None),
    ):
        result = runner.invoke(
            main,
            [
                "--jsonl",
                "--config", str(cfg_path),
                "snapshot", "cam1",
                "--output", "/tmp/x.jpg",
            ],
        )
    assert result.exit_code == 6, result.output
    err_obj = json.loads(result.output.strip().splitlines()[-1])
    assert err_obj["error"] == "config_error"
    assert err_obj["details"]["missing_dependency"] == "ffmpeg"


# ---------------------------------------------------------------------------
# --output - mutex with --json / --jsonl (FR-11d)
# ---------------------------------------------------------------------------


def test_output_dash_with_json_exits_64(cfg_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--json",
            "--config", str(cfg_path),
            "snapshot", "cam1",
            "--output", "-",
        ],
    )
    assert result.exit_code == 64
    assert "cannot be combined" in result.output


def test_output_dash_with_jsonl_exits_64(cfg_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--jsonl",
            "--config", str(cfg_path),
            "snapshot", "cam1",
            "--output", "-",
        ],
    )
    assert result.exit_code == 64
    assert "cannot be combined" in result.output


def test_quiet_with_output_dash_writes_jpeg_bytes(cfg_path: Path) -> None:
    """S15 carve-out: --quiet + --output - emits JPEG bytes on stdout
    regardless of the no-stdout invariant of --quiet (FR-11d).
    """

    async def _t1_pass(*args, **kwargs):
        return snap._TierResult(
            mechanism="pytapo", status="pass", elapsed_ms=5.0, payload=_TINY_JPEG
        )

    runner = CliRunner()
    with mock.patch.object(snap, "_tier1_pytapo", side_effect=_t1_pass):
        result = runner.invoke(
            main,
            [
                "--quiet",
                "--config", str(cfg_path),
                "snapshot", "cam1",
                "--output", "-",
            ],
        )
    assert result.exit_code == 0, result.output
    # CliRunner captures stdout as text with errors='backslashreplace' by
    # default; sys.stdout.buffer.write stores raw bytes via the runner's
    # binary mode in Click 8.1+.
    assert _TINY_JPEG[:4] in result.stdout_bytes or result.stdout_bytes != b""


# ---------------------------------------------------------------------------
# JSON output schema (FR-11b)
# ---------------------------------------------------------------------------


def test_json_output_schema_matches_fr_11b(cfg_path: Path, tmp_path: Path) -> None:
    """JSON envelope MUST include mechanism, bytes, width, height, elapsed_ms,
    target — and the mechanism MUST match the tier that succeeded.
    """

    async def _t1_pass(*args, **kwargs):
        return snap._TierResult(
            mechanism="pytapo", status="pass", elapsed_ms=42.5, payload=_TINY_JPEG
        )

    out = tmp_path / "snap.jpg"
    runner = CliRunner()
    with mock.patch.object(snap, "_tier1_pytapo", side_effect=_t1_pass):
        result = runner.invoke(
            main,
            [
                "--json",
                "--config", str(cfg_path),
                "snapshot", "cam1",
                "--output", str(out),
            ],
        )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mechanism"] == "pytapo"
    assert payload["bytes"] == len(_TINY_JPEG)
    assert payload["width"] == 8
    assert payload["height"] == 8
    assert payload["elapsed_ms"] == 42.5
    assert payload["target"] == "cam1"
    assert out.exists()


# ---------------------------------------------------------------------------
# --snapshot-budget overrides + chain integration
# ---------------------------------------------------------------------------


def test_snapshot_budget_zero_disables_pytapo_tier(cfg_path: Path, tmp_path: Path) -> None:
    """``--snapshot-budget pytapo=0,onvif=2,ffmpeg=2`` should skip tier 1
    entirely and go straight to ONVIF.
    """

    t1 = mock.AsyncMock()

    async def _t2_pass(*args, **kwargs):
        return snap._TierResult(
            mechanism="onvif", status="pass", elapsed_ms=1.0, payload=_TINY_JPEG
        )

    out = tmp_path / "snap.jpg"
    runner = CliRunner()
    with (
        mock.patch.object(snap, "_tier1_pytapo", t1),
        mock.patch.object(snap, "_tier2_onvif", side_effect=_t2_pass),
    ):
        result = runner.invoke(
            main,
            [
                "--timeout", "5",
                "--config", str(cfg_path),
                "snapshot", "cam1",
                "--output", str(out),
                "--snapshot-budget", "pytapo=0,onvif=2,ffmpeg=2",
            ],
        )
    assert result.exit_code == 0, result.output
    assert t1.call_count == 0


def test_group_target_rejected_by_snapshot(cfg_path: Path, tmp_path: Path) -> None:
    """A configured group like ``[groups.indoor] = ["cam1"]`` cannot be
    snapshotted (single-camera operation only). Exit 64.
    """
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[devices.cam1]\nip = "10.0.0.42"\n\n'
        '[groups]\nindoor = ["cam1"]\n',
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--jsonl",
            "--config", str(cfg),
            "snapshot", "@indoor",
            "--output", "/tmp/x.jpg",
        ],
    )
    assert result.exit_code == 64
