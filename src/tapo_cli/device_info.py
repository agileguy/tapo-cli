"""Shared device-metadata helpers (SRD §3.3, §3.3.1, §10.1).

Single source of truth for everything the verb modules need to project a
pytapo ``getBasicInfo`` response onto the §10.1 Camera record:

* :data:`MODEL_FEATURES` — per-model on-board feature set (§3.3.1).
* :func:`features_for_model` — model-string → feature list, deterministic.
* :func:`model_supported` — verified-list membership (§3.3).
* :func:`flatten_basic_info` — collapse the legacy / KLAP shapes into a
  single flat dict for downstream key lookups.
* :func:`format_mac` — normalize raw MAC strings to ``AA:BB:CC:DD:EE:FF``.

All functions here are pure — no I/O, no logging — so they're cheap to
call from anywhere and trivial to unit-test in isolation.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Capability matrix (SRD §3.3.1) — model prefix → on-board features
# ---------------------------------------------------------------------------

# Keyed by the family prefix the firmware reports in getBasicInfo's
# device_model. Empty cells in the SRD matrix mean the feature is
# unsupported on that model. The lookup uses ``startswith`` so trailing
# region tokens (e.g. ``C220 (EU)``) match the family entry.
MODEL_FEATURES: dict[str, frozenset[str]] = {
    "C100": frozenset({"led", "privacy", "ir"}),
    "C110": frozenset({"led", "privacy", "ir"}),
    "C200": frozenset({"led", "privacy", "ir", "ptz"}),
    "C210": frozenset({"led", "privacy", "ir", "audio", "ptz"}),
    "C220": frozenset({"led", "privacy", "ir", "audio", "ptz"}),
    "C225": frozenset({"led", "privacy", "ir", "audio", "ptz", "zoom", "dual-lens"}),
    "C320WS": frozenset({"led", "privacy", "ir", "audio", "alarm"}),
    "C420": frozenset({"led", "privacy", "ir", "audio", "alarm"}),
    "C520WS": frozenset({"led", "privacy", "ir", "audio", "alarm", "ptz"}),
    "C530WS": frozenset({"led", "privacy", "ir", "audio", "alarm", "ptz", "tts"}),
    "C710": frozenset({"led", "privacy", "ir", "audio", "alarm"}),
    "C720": frozenset({"led", "privacy", "ir", "audio", "alarm"}),
    "TC55": frozenset({"led", "privacy", "ir"}),
    "TC60": frozenset({"led", "privacy", "ir"}),
    "TC70": frozenset({"led", "privacy", "ir"}),
    "TC82": frozenset({"led", "privacy", "ir"}),
    "TC85": frozenset({"led", "privacy", "ir"}),
    "D100C": frozenset({"led", "privacy", "ir", "audio", "doorbell"}),
    "D210": frozenset({"led", "privacy", "ir", "audio", "doorbell"}),
    "D230": frozenset({"led", "privacy", "ir", "audio", "doorbell"}),
    "D235": frozenset({"led", "privacy", "ir", "audio", "doorbell"}),
}

# Verified-list (SRD §3.3) — the warning bar for the ``supported`` field.
# Includes the §3.3.1 matrix prefixes plus the README-listed models that
# don't have a distinct feature row yet (C120/C125/C201/C211/C216/C236,
# C310/C410/C500/C510W, C420S2).
VERIFIED_MODELS: frozenset[str] = frozenset(MODEL_FEATURES) | frozenset(
    {
        "C120", "C125", "C201", "C211", "C216", "C236",
        "C310", "C410", "C500", "C510W",
        "C420S2",
    }
)


def _normalize_model(model: str | None) -> str:
    """Drop trailing region tokens (e.g. ``"C220 (EU)"`` → ``"C220"``)."""
    if not model:
        return ""
    return model.upper().split()[0]


def features_for_model(model: str | None) -> list[str]:
    """Resolve the §3.3.1 feature set for a model string.

    Returns a sorted list (deterministic per §7.2). Unknown models return
    an empty list — the verb layer decides whether that's an error.
    """
    norm = _normalize_model(model)
    if not norm:
        return []
    for prefix, feats in MODEL_FEATURES.items():
        if norm.startswith(prefix):
            return sorted(feats)
    return []


def model_supported(model: str | None) -> bool:
    """``True`` iff ``model`` is on the v1 verified list (§3.3)."""
    norm = _normalize_model(model)
    if not norm:
        return False
    return any(norm.startswith(m) for m in VERIFIED_MODELS)


# ---------------------------------------------------------------------------
# pytapo response flattening
# ---------------------------------------------------------------------------


def flatten_basic_info(info: object) -> dict[str, Any]:
    """Flatten the multiple shapes of ``getBasicInfo`` into a flat dict.

    Three shapes occur in the field:

    * legacy: ``{"device_info": {"basic_info": {...}}}``
    * KLAP:   ``{"device_info": {...}}`` (fields directly under it)
    * flat:   already a single-level dict (some test fixtures, future fw)
    """
    if not isinstance(info, dict):
        return {}
    di = info.get("device_info")
    if isinstance(di, dict):
        bi = di.get("basic_info")
        if isinstance(bi, dict):
            return bi
        return di
    return info


def first_str(info: dict[str, Any], *keys: str) -> str:
    """First non-empty string value across candidate keys, else ``""``."""
    for k in keys:
        v = info.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def format_mac(raw: object) -> str:
    """Normalize a raw MAC into ``AA:BB:CC:DD:EE:FF``.

    Accepts colon-, dash-, and unseparated forms. Returns ``""`` for
    non-string input; returns the upper-cased original on any other shape
    (so callers can still surface what the device reported).
    """
    if not isinstance(raw, str):
        return ""
    cleaned = "".join(c for c in raw if c.isalnum()).upper()
    if len(cleaned) != 12:
        return raw.upper() if raw else ""
    return ":".join(cleaned[i : i + 2] for i in range(0, 12, 2))


__all__ = [
    "MODEL_FEATURES",
    "VERIFIED_MODELS",
    "features_for_model",
    "first_str",
    "flatten_basic_info",
    "format_mac",
    "model_supported",
]
