"""Discovery transports for tapo-cli (SRD §5.1, FR-1..7, B1).

Two co-equal primary paths, runnable in parallel by default:

* :func:`ws_discover` — ONVIF WS-Discovery multicast probe via the
  ``WSDiscovery`` library, wrapped in :func:`asyncio.to_thread` because
  ``searchServices`` is blocking.
* :func:`scan_subnet` — async TCP probe of ports 443/2020/554 across a
  CIDR. Tapo cameras consistently expose all three; the triple is a tight
  signature filter that keeps false positives low.

Multicast is silently dropped on most consumer mesh routers (§3.4), so
:func:`ws_discover` cannot be the only path in a real home deployment —
the scan path is co-equal primary, not a fallback.

Local-interface enumeration uses :mod:`ifaddr` (transitive via WSDiscovery).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger("tapo_cli")


# Tapo-camera signature ports: control plane TLS (443), legacy mgmt (2020),
# RTSP (554). Three concurrent successful TCP handshakes are a strong
# positive — random hosts won't have all three open.
TAPO_SIGNATURE_PORTS: tuple[int, ...] = (443, 2020, 554)


@dataclass(slots=True)
class DiscoveryHit:
    """One device responder, before final Camera-record projection.

    ``source`` records which transport observed the hit:
    ``"onvif"`` / ``"scan"`` / ``"both"`` after merge.
    """

    ip: str
    mac: str | None = None
    model: str | None = None
    firmware_version: str | None = None
    hardware_version: str | None = None
    source: str = "scan"
    open_ports: list[int] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Local interface enumeration (used by --target-network validation, FR-5b)
# ---------------------------------------------------------------------------


def local_ipv4_networks() -> list[ipaddress.IPv4Network]:
    """Return every non-loopback IPv4 network this host has an interface in.

    Used by the FR-5b ``--target-network`` validator: a CIDR with no
    matching local interface is exit code 6 with a hint listing these.
    """
    try:
        import ifaddr  # transitive via WSDiscovery
    except ImportError:  # pragma: no cover — ifaddr ships with WSDiscovery
        return []

    out: list[ipaddress.IPv4Network] = []
    for adapter in ifaddr.get_adapters():
        for entry in adapter.ips:
            if not isinstance(entry.ip, str):
                continue  # skip IPv6
            try:
                net = ipaddress.IPv4Network(
                    f"{entry.ip}/{entry.network_prefix}", strict=False
                )
            except (ValueError, ipaddress.AddressValueError):
                continue
            if net.is_loopback:
                continue
            out.append(net)
    return out


def primary_ipv4_network() -> ipaddress.IPv4Network | None:
    """Resolve the egress interface's /24 — used as the default scan target."""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Connecting a UDP socket to a public address makes the OS pick the
        # egress interface without sending any packet on the wire.
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()

    try:
        return ipaddress.IPv4Network(f"{ip}/24", strict=False)
    except (ValueError, ipaddress.AddressValueError):
        return None


# ---------------------------------------------------------------------------
# WS-Discovery transport
# ---------------------------------------------------------------------------

# scope strings carry the ONVIF model/manufacturer encoded as
# ``onvif://www.onvif.org/name/<model>`` etc. Tapo firmware encodes both.
_SCOPE_NAME_RE = re.compile(r"onvif://www\.onvif\.org/name/([^\s]+)", re.IGNORECASE)
_SCOPE_HW_RE = re.compile(
    r"onvif://www\.onvif\.org/hardware/([^\s]+)", re.IGNORECASE
)
# Match an IPv4 address inside a URL host:port path.
_XADDR_IPV4_RE = re.compile(r"https?://(\d+\.\d+\.\d+\.\d+)[:/]")


def _parse_ws_service(svc: object) -> DiscoveryHit | None:
    """Project one ``WSDiscovery`` service object onto a :class:`DiscoveryHit`."""
    get_xaddrs = getattr(svc, "getXAddrs", None)
    get_scopes = getattr(svc, "getScopes", None)
    if not (callable(get_xaddrs) and callable(get_scopes)):
        return None

    ip: str | None = None
    for x in get_xaddrs():
        match = _XADDR_IPV4_RE.match(str(x))
        if match:
            ip = match.group(1)
            break
    if ip is None:
        return None

    model: str | None = None
    hw: str | None = None
    for raw in get_scopes():
        text = str(raw)
        m = _SCOPE_NAME_RE.search(text)
        if m:
            model = m.group(1)
        h = _SCOPE_HW_RE.search(text)
        if h:
            hw = h.group(1)
    return DiscoveryHit(
        ip=ip,
        model=model,
        hardware_version=hw,
        source="onvif",
    )


async def ws_discover(*, timeout: float) -> list[DiscoveryHit]:
    """Run a WS-Discovery probe and return one hit per responder.

    The WSDiscovery library is sync and blocks on a UDP socket; we hand it
    to :func:`asyncio.to_thread` so it can't stall the event loop.

    Multicast bind failures (a multi-NIC host with no usable interface)
    surface as an empty list — never as an exception. The caller's job is
    to combine this with the scan path; an OS-level dual-failure is the
    only path that yields exit 3 (FR-5a).
    """

    def _blocking() -> list[DiscoveryHit]:
        try:
            from wsdiscovery import QName  # type: ignore[import-untyped]
            from wsdiscovery.discovery import (  # type: ignore[import-untyped]
                ThreadedWSDiscovery as WSDiscovery,
            )
        except ImportError:  # pragma: no cover
            return []

        wsd = WSDiscovery()
        try:
            wsd.start()
        except OSError as exc:
            logger.warning("ws-discovery: bind failed: %s", exc)
            return []

        # Constrain the probe to ONVIF NetworkVideoTransmitter so we don't
        # collect printers and SMB workstations.
        nvt = QName(
            "http://www.onvif.org/ver10/network/wsdl", "NetworkVideoTransmitter"
        )
        try:
            services = wsd.searchServices(types=[nvt], timeout=max(1, int(timeout)))
        except (OSError, RuntimeError) as exc:
            logger.warning("ws-discovery: search failed: %s", exc)
            services = []
        finally:
            with contextlib.suppress(Exception):
                wsd.stop()

        hits: list[DiscoveryHit] = []
        for svc in services:
            parsed = _parse_ws_service(svc)
            if parsed is not None:
                hits.append(parsed)
        return hits

    return await asyncio.to_thread(_blocking)


# ---------------------------------------------------------------------------
# Subnet TCP-signature scan
# ---------------------------------------------------------------------------


async def _probe_one_port(host: str, port: int, timeout: float) -> bool:
    """Single TCP connect attempt with timeout. ``True`` on success."""
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
    except (TimeoutError, OSError):
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    del reader
    return True


async def _probe_host(
    host: str,
    *,
    ports: tuple[int, ...],
    timeout: float,
) -> DiscoveryHit | None:
    """Probe a host on the signature port set. Hit iff ALL required ports open.

    Tapo cameras open 443, 2020, and 554 simultaneously. Random LAN hosts
    rarely satisfy all three; matching the full set keeps the scan
    precise. We probe in parallel per host so a slow port doesn't stretch
    the budget.
    """
    results = await asyncio.gather(
        *(_probe_one_port(host, p, timeout) for p in ports),
        return_exceptions=False,
    )
    open_ports = [p for p, is_open in zip(ports, results, strict=True) if is_open]
    if open_ports != list(ports):
        return None
    return DiscoveryHit(ip=host, source="scan", open_ports=open_ports)


async def scan_subnet(
    network: ipaddress.IPv4Network,
    *,
    timeout: float,
    concurrency: int = 64,
    ports: tuple[int, ...] = TAPO_SIGNATURE_PORTS,
) -> list[DiscoveryHit]:
    """Concurrently TCP-probe every host in ``network`` for the signature ports.

    Per-host probes run in parallel up to ``concurrency``. Per-port timeout
    is the supplied ``timeout`` divided across the three port probes
    inside the host (a slow port doesn't extend wall clock more than it
    must).
    """
    hosts: list[str] = [str(ip) for ip in network.hosts()]
    sem = asyncio.Semaphore(max(1, concurrency))

    # Per-port budget: keep each individual handshake snappy. Hosts that
    # don't have any ports open settle in the per-port timeout, not the
    # full --timeout.
    per_port_timeout = max(0.5, timeout / 6)

    async def _bounded(host: str) -> DiscoveryHit | None:
        async with sem:
            return await _probe_host(host, ports=ports, timeout=per_port_timeout)

    results = await asyncio.gather(*(_bounded(h) for h in hosts))
    return [hit for hit in results if hit is not None]


# ---------------------------------------------------------------------------
# Dedupe (FR-1a — MAC primary, IP fallback)
# ---------------------------------------------------------------------------


def dedupe_hits(hits: Iterable[DiscoveryHit]) -> list[DiscoveryHit]:
    """Merge hits by MAC (preferred) then IP. Stable, single pass.

    When the same device responds on both transports, the merged record
    carries ``source="both"`` and inherits ONVIF metadata (model/hw)
    where the scan record is missing it.
    """
    by_mac: dict[str, DiscoveryHit] = {}
    by_ip: dict[str, DiscoveryHit] = {}
    out: list[DiscoveryHit] = []

    for hit in hits:
        key_mac = hit.mac.upper() if hit.mac else None
        existing: DiscoveryHit | None = None
        if key_mac and key_mac in by_mac:
            existing = by_mac[key_mac]
        elif hit.ip in by_ip:
            existing = by_ip[hit.ip]

        if existing is None:
            if key_mac:
                by_mac[key_mac] = hit
            by_ip[hit.ip] = hit
            out.append(hit)
            continue

        # Merge: prefer non-None values from either side; bump source.
        existing.mac = existing.mac or hit.mac
        existing.model = existing.model or hit.model
        existing.firmware_version = existing.firmware_version or hit.firmware_version
        existing.hardware_version = existing.hardware_version or hit.hardware_version
        if hit.open_ports and not existing.open_ports:
            existing.open_ports = list(hit.open_ports)
        if existing.source != hit.source and existing.source != "both":
            existing.source = "both"

    return out


__all__ = [
    "TAPO_SIGNATURE_PORTS",
    "DiscoveryHit",
    "dedupe_hits",
    "local_ipv4_networks",
    "primary_ipv4_network",
    "scan_subnet",
    "ws_discover",
]
