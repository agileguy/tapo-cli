"""Tests for ``tapo-cli discover`` (FR-1..7, B1)."""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from tapo_cli import discovery
from tapo_cli.cli import main
from tapo_cli.discovery import DiscoveryHit, dedupe_hits


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path: Path):
    """Quarantine HOME / config dir per test (no real config bleed-through)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TAPO_CLI_CONFIG", raising=False)


# ---------------------------------------------------------------------------
# dedupe_hits — pure function
# ---------------------------------------------------------------------------


def test_dedupe_by_mac_merges_both_transports() -> None:
    onvif = DiscoveryHit(
        ip="192.168.1.10",
        mac="AA:BB:CC:DD:EE:01",
        model="C200",
        source="onvif",
    )
    scan = DiscoveryHit(
        ip="192.168.1.10",
        mac="AA:BB:CC:DD:EE:01",
        source="scan",
        open_ports=[443, 2020, 554],
    )
    merged = dedupe_hits([onvif, scan])
    assert len(merged) == 1
    assert merged[0].source == "both"
    assert merged[0].model == "C200"
    assert merged[0].open_ports == [443, 2020, 554]


def test_dedupe_by_ip_when_mac_missing() -> None:
    a = DiscoveryHit(ip="192.168.1.20", source="onvif", model="C220")
    b = DiscoveryHit(ip="192.168.1.20", source="scan", open_ports=[443, 2020, 554])
    merged = dedupe_hits([a, b])
    assert len(merged) == 1
    assert merged[0].model == "C220"
    assert merged[0].open_ports == [443, 2020, 554]


def test_dedupe_keeps_distinct_ips() -> None:
    a = DiscoveryHit(ip="192.168.1.30", source="scan")
    b = DiscoveryHit(ip="192.168.1.31", source="scan")
    assert len(dedupe_hits([a, b])) == 2


# ---------------------------------------------------------------------------
# CLI surface — flags + behaviour
# ---------------------------------------------------------------------------


def test_zero_results_exits_0_with_empty_array(monkeypatch) -> None:
    """FR-5a: zero responders within timeout → exit 0, ``[]`` in JSON mode."""

    async def _no_ws(*, timeout: float) -> list[DiscoveryHit]:
        return []

    async def _no_scan(network, *, timeout, concurrency=64, ports=()) -> list[DiscoveryHit]:
        return []

    monkeypatch.setattr(discovery, "ws_discover", _no_ws)
    monkeypatch.setattr(discovery, "scan_subnet", _no_scan)
    monkeypatch.setattr(
        discovery,
        "primary_ipv4_network",
        lambda: ipaddress.IPv4Network("192.168.1.0/24"),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["--json", "discover"])
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed == []


def test_default_runs_both_transports_and_merges(monkeypatch) -> None:
    seen: dict[str, bool] = {"ws": False, "scan": False}

    async def _fake_ws(*, timeout: float) -> list[DiscoveryHit]:
        seen["ws"] = True
        return [DiscoveryHit(ip="192.168.1.10", model="C200", source="onvif")]

    async def _fake_scan(network, *, timeout, concurrency=64, ports=()) -> list[DiscoveryHit]:
        seen["scan"] = True
        return [
            DiscoveryHit(
                ip="192.168.1.10", source="scan", open_ports=[443, 2020, 554]
            )
        ]

    monkeypatch.setattr(discovery, "ws_discover", _fake_ws)
    monkeypatch.setattr(discovery, "scan_subnet", _fake_scan)
    monkeypatch.setattr(
        discovery,
        "primary_ipv4_network",
        lambda: ipaddress.IPv4Network("192.168.1.0/24"),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["--json", "discover"])
    assert result.exit_code == 0, result.output
    assert seen == {"ws": True, "scan": True}
    parsed = json.loads(result.stdout)
    assert len(parsed) == 1
    assert parsed[0]["ip"] == "192.168.1.10"
    assert parsed[0]["source"] == "both"


def test_no_scan_skips_scan_path(monkeypatch) -> None:
    seen: dict[str, bool] = {"ws": False, "scan": False}

    async def _fake_ws(*, timeout: float) -> list[DiscoveryHit]:
        seen["ws"] = True
        return []

    async def _fake_scan(network, *, timeout, concurrency=64, ports=()) -> list[DiscoveryHit]:
        seen["scan"] = True
        return []

    monkeypatch.setattr(discovery, "ws_discover", _fake_ws)
    monkeypatch.setattr(discovery, "scan_subnet", _fake_scan)

    runner = CliRunner()
    result = runner.invoke(main, ["--json", "discover", "--no-scan"])
    assert result.exit_code == 0
    assert seen == {"ws": True, "scan": False}


def test_ws_discovery_only_is_alias_of_no_scan(monkeypatch) -> None:
    seen: dict[str, bool] = {"ws": False, "scan": False}

    async def _fake_ws(*, timeout: float) -> list[DiscoveryHit]:
        seen["ws"] = True
        return []

    async def _fake_scan(network, *, timeout, concurrency=64, ports=()) -> list[DiscoveryHit]:
        seen["scan"] = True
        return []

    monkeypatch.setattr(discovery, "ws_discover", _fake_ws)
    monkeypatch.setattr(discovery, "scan_subnet", _fake_scan)

    runner = CliRunner()
    result = runner.invoke(main, ["--json", "discover", "--ws-discovery-only"])
    assert result.exit_code == 0
    assert seen == {"ws": True, "scan": False}


def test_scan_only_skips_ws_discovery(monkeypatch) -> None:
    seen: dict[str, bool] = {"ws": False, "scan": False}

    async def _fake_ws(*, timeout: float) -> list[DiscoveryHit]:
        seen["ws"] = True
        return []

    async def _fake_scan(network, *, timeout, concurrency=64, ports=()) -> list[DiscoveryHit]:
        seen["scan"] = True
        return []

    monkeypatch.setattr(discovery, "ws_discover", _fake_ws)
    monkeypatch.setattr(discovery, "scan_subnet", _fake_scan)
    monkeypatch.setattr(
        discovery,
        "primary_ipv4_network",
        lambda: ipaddress.IPv4Network("192.168.1.0/24"),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["--json", "discover", "--scan-only"])
    assert result.exit_code == 0
    assert seen == {"ws": False, "scan": True}


def test_target_network_with_no_local_iface_exits_6(monkeypatch) -> None:
    """FR-5b: --target-network outside any local interface CIDR exits 6."""
    monkeypatch.setattr(
        discovery,
        "local_ipv4_networks",
        lambda: [ipaddress.IPv4Network("192.168.86.0/24")],
    )

    runner = CliRunner()
    result = runner.invoke(
        main, ["--json", "discover", "--target-network", "10.0.0.0/24"]
    )
    assert result.exit_code == 6, result.output
    err_payload = json.loads(result.output.strip().splitlines()[-1])
    assert err_payload["error"] == "config_error"
    assert "no matching local interface" in err_payload["message"]


def test_invalid_target_network_cidr_exits_6() -> None:
    runner = CliRunner()
    result = runner.invoke(
        main, ["--json", "discover", "--target-network", "not-a-cidr"]
    )
    assert result.exit_code == 6
    err_payload = json.loads(result.output.strip().splitlines()[-1])
    assert err_payload["error"] == "config_error"


def test_scan_and_no_scan_mutex_is_usage_error() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["discover", "--no-scan", "--scan-only"])
    assert result.exit_code == 64


def test_target_network_inside_local_iface_runs(monkeypatch) -> None:
    """A CIDR inside (or matching) a local interface SHOULD run, not 6."""
    captured: dict[str, ipaddress.IPv4Network] = {}

    async def _fake_ws(*, timeout: float) -> list[DiscoveryHit]:
        return []

    async def _fake_scan(
        network: ipaddress.IPv4Network, *, timeout, concurrency=64, ports=()
    ) -> list[DiscoveryHit]:
        captured["net"] = network
        return []

    monkeypatch.setattr(discovery, "ws_discover", _fake_ws)
    monkeypatch.setattr(discovery, "scan_subnet", _fake_scan)
    monkeypatch.setattr(
        discovery,
        "local_ipv4_networks",
        lambda: [ipaddress.IPv4Network("192.168.86.0/24")],
    )

    runner = CliRunner()
    result = runner.invoke(
        main, ["--json", "discover", "--target-network", "192.168.86.0/24"]
    )
    assert result.exit_code == 0
    assert captured["net"] == ipaddress.IPv4Network("192.168.86.0/24")


def test_record_shape_matches_section_10_1(monkeypatch) -> None:
    async def _fake_ws(*, timeout: float) -> list[DiscoveryHit]:
        return [
            DiscoveryHit(
                ip="192.168.1.42",
                mac="AA:BB:CC:DD:EE:01",
                model="C225",
                source="onvif",
            )
        ]

    async def _fake_scan(network, *, timeout, concurrency=64, ports=()) -> list[DiscoveryHit]:
        return []

    monkeypatch.setattr(discovery, "ws_discover", _fake_ws)
    monkeypatch.setattr(discovery, "scan_subnet", _fake_scan)
    monkeypatch.setattr(
        discovery,
        "primary_ipv4_network",
        lambda: ipaddress.IPv4Network("192.168.1.0/24"),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["--json", "discover", "--no-scan"])
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert len(parsed) == 1
    rec = parsed[0]
    # SRD §10.1 keys, plus the discover-only ``source``/``open_ports`` extras.
    for key in (
        "alias",
        "ip",
        "mac",
        "model",
        "hardware_version",
        "firmware_version",
        "supported",
        "features",
        "motion_enabled",
        "privacy_enabled",
        "led_state",
        "night_vision_mode",
        "has_camera_account",
        "last_seen",
    ):
        assert key in rec, f"§10.1 key {key!r} missing"
    assert rec["supported"] is True  # C225 is on the verified list
    assert rec["last_seen"].endswith("Z")
