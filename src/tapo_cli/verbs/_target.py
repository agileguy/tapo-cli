"""Shared target-resolution helper for Phase 1d state-control verbs.

Pulled out of the per-verb modules to keep the privacy/led/night-vision/
motion/reboot files small and focused on their pytapo call. The behaviour
mirrors :mod:`tapo_cli.verbs.info_cmd`: resolve a TARGET (alias, ``@alias``,
or bare IPv4) into a config :class:`~tapo_cli.config.Config` that the
wrapper can use, synthesizing a ``DeviceEntry`` for bare IPs that aren't
in config so the wrapper's ``resolve_target`` accepts them.

Group fan-out (``@group``) is **NOT** handled here — Phase 1d follows
the Phase 1b precedent of treating ``@alias`` as a single alias (the
leading ``@`` is stripped). Real group expansion lands in a later phase
once the cross-cutting parallel-execution layer is in place.
"""

from __future__ import annotations

from pathlib import Path

from tapo_cli.config import Config, DeviceEntry, load_config


def load_config_with_target(target: str, config_path: object) -> tuple[Config, str]:
    """Load the config and ensure ``target`` is resolvable by the wrapper.

    Returns the (possibly augmented) :class:`Config` and the canonical
    target string with any leading ``@`` stripped. Mirrors
    :func:`tapo_cli.verbs.info_cmd._ensure_target_resolvable`.
    """
    resolved = target.lstrip("@") or target
    cfg_path = Path(str(config_path)).expanduser() if config_path else None
    cfg = load_config(cfg_path)

    if resolved not in cfg.devices and _looks_like_ipv4(resolved):
        cfg.devices[resolved] = DeviceEntry(alias=resolved, ip=resolved)

    return cfg, resolved


def _looks_like_ipv4(s: str) -> bool:
    parts = s.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


__all__ = ["load_config_with_target"]
