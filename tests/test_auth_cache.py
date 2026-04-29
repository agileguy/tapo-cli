"""Tests for the pytapo session cache (SRD §6.5, FR-CRED-9..13)."""

from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path

import pytest

from tapo_cli import auth_cache
from tapo_cli.errors import NetworkError


@pytest.fixture(autouse=True)
def _redirect_cache(monkeypatch, tmp_path: Path):
    """Redirect cache root so tests don't touch the user's real ~/.config."""
    monkeypatch.setenv(auth_cache.ENV_CONFIG_DIR, str(tmp_path / "tapo-cli"))


# ---------------------------------------------------------------------------
# Save / load round-trip
# ---------------------------------------------------------------------------


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    state = {"foo": "bar", "nested": {"a": 1}}
    auth_cache.save_session("AA:BB:CC:DD:EE:01", state, pytapo_version="0.0.test")

    path = auth_cache.cache_path_for_mac("AA:BB:CC:DD:EE:01")
    assert path.exists()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600

    loaded = auth_cache.load_session("AA:BB:CC:DD:EE:01", pytapo_version="0.0.test")
    assert loaded == state


def test_save_uses_atomic_rename(monkeypatch, tmp_path: Path) -> None:
    """Atomic write: the target file MUST appear via os.replace, not a partial write."""
    seen_temps: list[str] = []
    real_replace = os.replace

    def watch_replace(src, dst):
        seen_temps.append(str(src))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", watch_replace)

    auth_cache.save_session("AA:BB:CC:DD:EE:02", {"x": 1}, pytapo_version="0.0.test")
    assert seen_temps, "save_session must call os.replace exactly once"
    assert seen_temps[0].endswith(".tmp")


def test_save_chmods_dir_to_0700(tmp_path: Path) -> None:
    auth_cache.save_session("AA:BB:CC:DD:EE:03", {"x": 1}, pytapo_version="0.0.test")
    tokens = auth_cache.cache_dir()
    mode = stat.S_IMODE(tokens.stat().st_mode)
    assert mode == 0o700


# ---------------------------------------------------------------------------
# Pytapo version mismatch invalidation (FR-CRED-9)
# ---------------------------------------------------------------------------


def test_pytapo_version_mismatch_invalidates_cache(tmp_path: Path, caplog) -> None:
    import logging

    auth_cache.save_session("AA:BB:CC:DD:EE:04", {"x": 1}, pytapo_version="1.0.0")

    with caplog.at_level(logging.INFO, logger="tapo_cli"):
        result = auth_cache.load_session(
            "AA:BB:CC:DD:EE:04",
            pytapo_version="2.0.0",
        )
    assert result is None
    # Cache file deleted.
    assert not auth_cache.cache_path_for_mac("AA:BB:CC:DD:EE:04").exists()
    assert any("pytapo_version mismatch" in r.getMessage() for r in caplog.records)


def test_credential_source_mismatch_invalidates_cache(tmp_path: Path) -> None:
    """FR-CRED-11: changing --credential-source invalidates the cache."""
    auth_cache.save_session(
        "AA:BB:CC:DD:EE:05",
        {"x": 1},
        pytapo_version="0.0.test",
        credential_source="cloud_account",
    )
    out = auth_cache.load_session(
        "AA:BB:CC:DD:EE:05",
        pytapo_version="0.0.test",
        credential_source="camera_account",
    )
    assert out is None
    assert not auth_cache.cache_path_for_mac("AA:BB:CC:DD:EE:05").exists()


def test_corrupt_cache_dropped_silently(tmp_path: Path) -> None:
    path = auth_cache.cache_path_for_mac("AA:BB:CC:DD:EE:06")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not valid json {{{", encoding="utf-8")
    os.chmod(path, 0o600)
    out = auth_cache.load_session(
        "AA:BB:CC:DD:EE:06", pytapo_version="0.0.test"
    )
    assert out is None
    assert not path.exists()


# ---------------------------------------------------------------------------
# Per-device flock (FR-CRED-13)
# ---------------------------------------------------------------------------


def test_lock_for_write_blocks_concurrent_writer(tmp_path: Path) -> None:
    """Two threads writing the same MAC serialize on the lock."""
    mac = "AA:BB:CC:DD:EE:07"
    holding = threading.Event()
    released = threading.Event()

    def first() -> None:
        with auth_cache.lock_for_write(mac, timeout=1.0):
            holding.set()
            time.sleep(0.05)
            released.set()

    t = threading.Thread(target=first)
    t.start()
    holding.wait(timeout=1.0)
    # Second acquire MUST wait for the first to release.
    with auth_cache.lock_for_write(mac, timeout=1.0):
        assert released.is_set()
    t.join()


def test_lock_timeout_exits_3(tmp_path: Path) -> None:
    """FR-CRED-13: lock timeout raises NetworkError (exit 3), not AuthError."""
    mac = "AA:BB:CC:DD:EE:08"
    holding = threading.Event()
    proceed = threading.Event()

    def hold() -> None:
        with auth_cache.lock_for_write(mac, timeout=1.0):
            holding.set()
            proceed.wait(timeout=2.0)

    t = threading.Thread(target=hold)
    t.start()
    holding.wait(timeout=1.0)
    try:
        with pytest.raises(NetworkError) as ei, auth_cache.lock_for_write(mac, timeout=0.1):
            pass
        assert ei.value.exit_code == 3
        assert "timed out" in ei.value.message
    finally:
        proceed.set()
        t.join()


def test_negative_timeout_rejected(tmp_path: Path) -> None:
    from tapo_cli.errors import ConfigError

    with pytest.raises(ConfigError), auth_cache.lock_for_write("AA:BB:CC:DD:EE:09", timeout=-1):
        pass


# ---------------------------------------------------------------------------
# Flush / list
# ---------------------------------------------------------------------------


def test_flush_one_returns_false_on_miss(tmp_path: Path) -> None:
    assert auth_cache.flush_one("11:22:33:44:55:66") is False


def test_flush_all_removes_only_json_files(tmp_path: Path) -> None:
    auth_cache.save_session("AA:BB:CC:DD:EE:10", {"x": 1}, pytapo_version="0.0.test")
    auth_cache.save_session("AA:BB:CC:DD:EE:11", {"x": 1}, pytapo_version="0.0.test")
    # Sibling .lock files MUST NOT be unlinked by flush_all.
    cache = auth_cache.cache_dir()
    (cache / "AA:BB:CC:DD:EE:10.lock").touch()

    removed = auth_cache.flush_all()
    assert removed == 2
    assert (cache / "AA:BB:CC:DD:EE:10.lock").exists()


def test_list_session_files_returns_only_json(tmp_path: Path) -> None:
    auth_cache.save_session("AA:BB:CC:DD:EE:12", {"x": 1}, pytapo_version="0.0.test")
    cache = auth_cache.cache_dir()
    (cache / "garbage.txt").touch()
    files = auth_cache.list_session_files()
    assert len(files) == 1
    assert files[0].suffix == ".json"


def test_read_session_meta_extracts_top_level_fields(tmp_path: Path) -> None:
    auth_cache.save_session(
        "AA:BB:CC:DD:EE:13",
        {"opaque": "blob"},
        pytapo_version="9.9.9",
        expires_at="2026-01-01T00:00:00Z",
        credential_source="camera_account",
    )
    p = auth_cache.cache_path_for_mac("AA:BB:CC:DD:EE:13")
    meta = auth_cache.read_session_meta(p)
    assert meta[auth_cache.KEY_PYTAPO_VERSION] == "9.9.9"
    assert meta[auth_cache.KEY_EXPIRES_AT] == "2026-01-01T00:00:00Z"
    assert meta[auth_cache.KEY_SOURCE] == "camera_account"


def test_mtime_rfc3339_has_z_suffix(tmp_path: Path) -> None:
    auth_cache.save_session("AA:BB:CC:DD:EE:14", {"x": 1}, pytapo_version="0.0.test")
    p = auth_cache.cache_path_for_mac("AA:BB:CC:DD:EE:14")
    s = auth_cache.mtime_rfc3339(p)
    assert s.endswith("Z"), s
    assert "+" not in s
    assert "T" in s


# ---------------------------------------------------------------------------
# Two consecutive auth failures → exit 2 (FR-CRED-11)
# ---------------------------------------------------------------------------


def test_auth_retry_exhausted_error_is_auth_error_subclass() -> None:
    """FR-CRED-11: AuthRetryExhaustedError exits 2 — used by wrapper retry path."""
    from tapo_cli.errors import AuthError

    exc = auth_cache.AuthRetryExhaustedError("two strikes")
    assert isinstance(exc, AuthError)
    assert exc.exit_code == 2


# ---------------------------------------------------------------------------
# Mac normalization (regression)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("aa:bb:cc:dd:ee:ff", "AA:BB:CC:DD:EE:FF"),
        ("aa-bb-cc-dd-ee-ff", "AA:BB:CC:DD:EE:FF"),
        ("aabbccddeeff", "AA:BB:CC:DD:EE:FF"),
    ],
)
def test_path_for_mac_normalized(raw: str, expected: str) -> None:
    p = auth_cache.cache_path_for_mac(raw)
    assert p.name == f"{expected}.json"


# ---------------------------------------------------------------------------
# Concurrent reads do NOT take the lock — readers proceed during a write
# ---------------------------------------------------------------------------


def test_load_session_does_not_block_on_lock(tmp_path: Path) -> None:
    """FR-CRED-13: reads do NOT take the lock; only writes do."""
    mac = "AA:BB:CC:DD:EE:15"
    auth_cache.save_session(mac, {"x": 1}, pytapo_version="0.0.test")

    holding = threading.Event()
    proceed = threading.Event()

    def hold_writer_lock() -> None:
        with auth_cache.lock_for_write(mac, timeout=1.0):
            holding.set()
            proceed.wait(timeout=2.0)

    t = threading.Thread(target=hold_writer_lock)
    t.start()
    holding.wait(timeout=1.0)
    try:
        # Reader must succeed even while the write lock is held.
        out = auth_cache.load_session(mac, pytapo_version="0.0.test")
        assert out == {"x": 1}
    finally:
        proceed.set()
        t.join()
