"""``tapo-cli discover`` (SRD §5.1, FR-1..7, B1).

Run WS-Discovery and a subnet TCP-signature scan **concurrently** by
default — multicast is silently dropped on most consumer mesh routers
(§3.4) so the scan path is co-equal primary, not a fallback.

Result records are projected onto the §10.1 Camera record shape with
``alias`` left blank (no config resolution at the discover layer).
``--probe`` issues a pytapo ``getBasicInfo`` per hit to enrich the
model / firmware fields — slow, opt-in.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import sys

import click

from tapo_cli import discovery
from tapo_cli.config import load_config
from tapo_cli.device_info import flatten_basic_info, format_mac, model_supported
from tapo_cli.discovery import DiscoveryHit
from tapo_cli.errors import (
    EXIT_SUCCESS,
    EXIT_USAGE_ERROR,
    ConfigError,
    StructuredError,
    TapoCliError,
)
from tapo_cli.output import OutputMode, emit_error, emit_stream, utc_now_rfc3339
from tapo_cli.runner import run_async as _run_async

logger = logging.getLogger("tapo_cli")


# ---------------------------------------------------------------------------
# Hit → §10.1 Camera record projection
# ---------------------------------------------------------------------------


def _hit_to_record(hit: DiscoveryHit) -> dict[str, object]:
    """Project a :class:`DiscoveryHit` onto the §10.1 Camera-record shape.

    ``alias`` stays empty at the discover layer — config resolution
    happens at ``info``/``list`` time. Live-state fields the discover path
    can't know (motion/privacy/led/night-vision) emit as ``unknown``-style
    placeholders, matching SRD §10.1's stable-key requirement.
    """
    return {
        "alias": "",
        "ip": hit.ip,
        "mac": (hit.mac or "").upper(),
        "model": hit.model or "",
        "hardware_version": hit.hardware_version or "",
        "firmware_version": hit.firmware_version or "",
        "supported": model_supported(hit.model),
        "features": [],
        "motion_enabled": False,
        "privacy_enabled": False,
        "led_state": "off",
        "night_vision_mode": "unknown",
        "has_camera_account": False,
        "last_seen": utc_now_rfc3339(),
        "source": hit.source,
        "open_ports": list(hit.open_ports),
    }


def _record_to_text(record: object) -> str:
    """Fixed-column text rendering: ip, mac, model, firmware, supported."""
    assert isinstance(record, dict)
    return (
        f"{record.get('ip', ''):<15} "
        f"{record.get('mac', ''):<17} "
        f"{record.get('model', '') or '-':<10} "
        f"{record.get('firmware_version', '') or '-':<14} "
        f"supported={'y' if record.get('supported') else 'n'}"
    )


# ---------------------------------------------------------------------------
# Click verb
# ---------------------------------------------------------------------------


@click.command("discover")
@click.option(
    "--no-scan",
    "no_scan",
    is_flag=True,
    default=False,
    help="WS-Discovery only; skip the subnet TCP-signature scan.",
)
@click.option(
    "--ws-discovery-only",
    "ws_only",
    is_flag=True,
    default=False,
    help="Alias of --no-scan.",
)
@click.option(
    "--scan-only",
    "scan_only",
    is_flag=True,
    default=False,
    help="Subnet TCP-signature scan only; skip WS-Discovery (FR-1d).",
)
@click.option(
    "--target-network",
    "target_network",
    type=str,
    default=None,
    help="Restrict scan to an explicit CIDR (FR-5/5b).",
)
@click.option(
    "--probe",
    "probe",
    is_flag=True,
    default=False,
    help="Also fetch model + firmware via pytapo getBasicInfo (slow).",
)
@click.pass_context
def discover_cmd(
    ctx: click.Context,
    *,
    no_scan: bool,
    ws_only: bool,
    scan_only: bool,
    target_network: str | None,
    probe: bool,
) -> None:
    """Discover Tapo cameras on the LAN.

    Runs ONVIF WS-Discovery and a subnet TCP-signature scan concurrently
    by default. Per FR-5a a zero-result run is success (exit 0, empty
    output).
    """
    state = ctx.obj
    mode: OutputMode = state["mode"]

    skip_scan = no_scan or ws_only
    if skip_scan and scan_only:
        err = StructuredError(
            error="usage_error",
            exit_code=EXIT_USAGE_ERROR,
            message="--scan-only is mutually exclusive with --no-scan/--ws-discovery-only",
        )
        emit_error(err, mode)
        sys.exit(EXIT_USAGE_ERROR)

    timeout = float(state.get("timeout") or 5.0)

    rc = _run_async(
        lambda: _run(
            mode=mode,
            timeout=timeout,
            do_scan=not skip_scan,
            do_ws=not scan_only,
            target_network=target_network,
            probe=probe,
            config_path=state.get("config_path"),
        ),
        mode=mode,
    )
    sys.exit(rc)


async def _run(
    *,
    mode: OutputMode,
    timeout: float,
    do_scan: bool,
    do_ws: bool,
    target_network: str | None,
    probe: bool,
    config_path: object,
) -> int:
    """Async core: fan out the two transports, dedupe, project, emit."""

    # CIDR validation up front — exit 6 if the user picked a network with no
    # local interface in it (FR-5b).
    chosen_network: ipaddress.IPv4Network | None = None
    if target_network is not None:
        try:
            chosen_network = ipaddress.IPv4Network(target_network, strict=False)
        except (ValueError, ipaddress.AddressValueError) as exc:
            raise ConfigError(
                f"--target-network is not a valid IPv4 CIDR: {target_network!r}",
                hint="Pass like --target-network 192.168.1.0/24",
            ) from exc

        local_nets = discovery.local_ipv4_networks()
        if not any(chosen_network.overlaps(ln) for ln in local_nets):
            available = ", ".join(str(n) for n in local_nets) or "<none>"
            raise ConfigError(
                f"--target-network {chosen_network} has no matching local interface",
                hint=f"Available interface networks: {available}",
                extra={"available": [str(n) for n in local_nets]},
            )

    # Fan out: kick off both tasks if requested, then gather.
    tasks: list[asyncio.Task[list[DiscoveryHit]]] = []
    if do_ws:
        tasks.append(asyncio.create_task(discovery.ws_discover(timeout=timeout)))
    if do_scan:
        scan_net = chosen_network or discovery.primary_ipv4_network()
        if scan_net is None:
            raise ConfigError(
                "could not determine local subnet for --scan",
                hint="Pass --target-network <CIDR> explicitly.",
            )
        tasks.append(
            asyncio.create_task(discovery.scan_subnet(scan_net, timeout=timeout))
        )

    if not tasks:
        # Both transports disabled — usage error.
        raise ConfigError(
            "all discovery transports were disabled",
            hint="Drop --no-scan or --scan-only.",
        )

    raw_hits: list[DiscoveryHit] = []
    for batch in await asyncio.gather(*tasks, return_exceptions=False):
        raw_hits.extend(batch)

    merged = discovery.dedupe_hits(raw_hits)

    if probe and merged:
        await _enrich_with_pytapo(merged, config_path=config_path, timeout=timeout)

    if not merged:
        # FR-5a: zero responders within timeout is success, not error.
        sys.stderr.write(f"INFO timeout reached, 0 devices found (timeout={timeout:g}s)\n")

    records = [_hit_to_record(hit) for hit in merged]
    emit_stream(records, mode, formatter=_record_to_text)
    return EXIT_SUCCESS


async def _enrich_with_pytapo(
    hits: list[DiscoveryHit],
    *,
    config_path: object,
    timeout: float,
) -> None:
    """Best-effort getBasicInfo per hit to fill in model + firmware.

    Failures (auth, network, pytapo missing) leave the hit unchanged —
    discover --probe is opportunistic, not a hard guarantee.
    """
    from pathlib import Path

    from tapo_cli import wrapper as wrap
    from tapo_cli.credentials import resolve_control_plane

    cfg_path = Path(str(config_path)).expanduser() if config_path else None
    try:
        cfg = load_config(cfg_path)
    except TapoCliError as exc:
        logger.info("discover --probe: skipping enrichment, no config: %s", exc.message)
        return

    cred = resolve_control_plane(cfg, alias=None, source=None)
    if cred is None:
        logger.info("discover --probe: skipping enrichment, no credentials configured")
        return

    async def _enrich_one(hit: DiscoveryHit) -> None:
        target = wrap.TapoTarget(alias=hit.ip, ip=hit.ip)
        try:
            tapo = await wrap._build_tapo(target, cred)
        except TapoCliError:
            return

        def _basic() -> dict[str, object]:
            try:
                result: object = tapo.getBasicInfo()
            except Exception:  # pragma: no cover — defensive
                return {}
            return result if isinstance(result, dict) else {}

        info = await asyncio.to_thread(_basic)
        flat = flatten_basic_info(info)
        if not hit.model and flat.get("device_model"):
            hit.model = str(flat["device_model"])
        if not hit.firmware_version and flat.get("fw_version"):
            hit.firmware_version = str(flat["fw_version"])
        if not hit.hardware_version and flat.get("hw_version"):
            hit.hardware_version = str(flat["hw_version"])
        if not hit.mac and flat.get("mac"):
            hit.mac = format_mac(flat["mac"])

    await asyncio.gather(*(_enrich_one(h) for h in hits))
    del timeout  # reserved


__all__ = ["discover_cmd"]
