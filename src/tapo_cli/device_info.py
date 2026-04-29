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


# ---------------------------------------------------------------------------
# Per-feature x per-model capability matrix (SRD §3.3.1, S4)
# ---------------------------------------------------------------------------
#
# This is the contract Phase 2 verbs (ptz, preset, alarm, audio, osd) consult
# BEFORE issuing any pytapo call. Each entry maps a model family prefix to a
# capability dict. Verbs call :func:`feature_supported` and exit code 5 with a
# structured hint listing the supported-models set when the gate fails.
#
# Capability key reference (mirrors the §3.3.1 table column names):
#
# * ``ptz_mode``        — ``"none"`` | ``"step"`` | ``"continuous"``
# * ``zoom``            — bool (camera has a zoom motor distinct from pan/tilt)
# * ``preset``          — bool (savePreset / setPreset / deletePreset usable)
# * ``alarm``           — bool (siren / alarm config usable)
# * ``alarm_trigger``   — bool (startManualAlarm usable)
# * ``audio_mic``       — bool (setMicrophone usable)
# * ``audio_speaker``   — bool (setSpeakerVolume usable; alarm-only or full)
# * ``audio_tts``       — bool (TTS playback supported)
# * ``osd_text``        — bool (custom label overlay supported)
# * ``osd_timestamp``   — bool (date/time overlay toggle supported)
#
# C200 (live test target) carries continuous-PTZ in some references, but the
# SRD §3.3.1 matrix pins C200 to ``ptz_mode: "step"`` — we honor the SRD.

MODEL_CAPABILITIES: dict[str, dict[str, object]] = {
    "C100": {
        "ptz_mode": "none", "zoom": False, "preset": False, "alarm": False,
        "alarm_trigger": False, "audio_mic": False, "audio_speaker": False,
        "audio_tts": False, "osd_text": False, "osd_timestamp": True,
    },
    "C110": {
        "ptz_mode": "none", "zoom": False, "preset": False, "alarm": False,
        "alarm_trigger": False, "audio_mic": False, "audio_speaker": False,
        "audio_tts": False, "osd_text": False, "osd_timestamp": True,
    },
    "C200": {
        "ptz_mode": "step", "zoom": False, "preset": True, "alarm": True,
        "alarm_trigger": False, "audio_mic": True, "audio_speaker": True,
        "audio_tts": False, "osd_text": False, "osd_timestamp": True,
    },
    "C210": {
        "ptz_mode": "step", "zoom": False, "preset": True, "alarm": False,
        "alarm_trigger": False, "audio_mic": True, "audio_speaker": True,
        "audio_tts": False, "osd_text": False, "osd_timestamp": True,
    },
    "C220": {
        "ptz_mode": "step", "zoom": False, "preset": True, "alarm": False,
        "alarm_trigger": False, "audio_mic": True, "audio_speaker": True,
        "audio_tts": False, "osd_text": False, "osd_timestamp": True,
    },
    "C225": {
        "ptz_mode": "continuous", "zoom": True, "preset": True, "alarm": False,
        "alarm_trigger": False, "audio_mic": True, "audio_speaker": True,
        "audio_tts": False, "osd_text": True, "osd_timestamp": True,
    },
    "C320WS": {
        "ptz_mode": "none", "zoom": False, "preset": False, "alarm": True,
        "alarm_trigger": True, "audio_mic": True, "audio_speaker": True,
        "audio_tts": False, "osd_text": True, "osd_timestamp": True,
    },
    "C420": {
        "ptz_mode": "none", "zoom": False, "preset": False, "alarm": True,
        "alarm_trigger": True, "audio_mic": True, "audio_speaker": True,
        "audio_tts": False, "osd_text": True, "osd_timestamp": True,
    },
    "C520WS": {
        "ptz_mode": "step", "zoom": False, "preset": True, "alarm": True,
        "alarm_trigger": True, "audio_mic": True, "audio_speaker": True,
        "audio_tts": True, "osd_text": True, "osd_timestamp": True,
    },
    "C530WS": {
        "ptz_mode": "step", "zoom": False, "preset": True, "alarm": True,
        "alarm_trigger": True, "audio_mic": True, "audio_speaker": True,
        "audio_tts": True, "osd_text": True, "osd_timestamp": True,
    },
    "C710": {
        "ptz_mode": "none", "zoom": False, "preset": False, "alarm": True,
        "alarm_trigger": True, "audio_mic": True, "audio_speaker": True,
        "audio_tts": False, "osd_text": True, "osd_timestamp": True,
    },
    "C720": {
        "ptz_mode": "none", "zoom": False, "preset": False, "alarm": True,
        "alarm_trigger": True, "audio_mic": True, "audio_speaker": True,
        "audio_tts": False, "osd_text": True, "osd_timestamp": True,
    },
    "TC55": {
        "ptz_mode": "none", "zoom": False, "preset": False, "alarm": False,
        "alarm_trigger": False, "audio_mic": False, "audio_speaker": False,
        "audio_tts": False, "osd_text": False, "osd_timestamp": True,
    },
    "TC60": {
        "ptz_mode": "none", "zoom": False, "preset": False, "alarm": False,
        "alarm_trigger": False, "audio_mic": False, "audio_speaker": False,
        "audio_tts": False, "osd_text": False, "osd_timestamp": True,
    },
    "TC70": {
        "ptz_mode": "none", "zoom": False, "preset": False, "alarm": False,
        "alarm_trigger": False, "audio_mic": False, "audio_speaker": False,
        "audio_tts": False, "osd_text": False, "osd_timestamp": True,
    },
    "TC82": {
        "ptz_mode": "none", "zoom": False, "preset": False, "alarm": False,
        "alarm_trigger": False, "audio_mic": False, "audio_speaker": False,
        "audio_tts": False, "osd_text": False, "osd_timestamp": True,
    },
    "TC85": {
        "ptz_mode": "none", "zoom": False, "preset": False, "alarm": False,
        "alarm_trigger": False, "audio_mic": False, "audio_speaker": False,
        "audio_tts": False, "osd_text": False, "osd_timestamp": True,
    },
    "D100C": {
        "ptz_mode": "none", "zoom": False, "preset": False, "alarm": False,
        "alarm_trigger": False, "audio_mic": True, "audio_speaker": True,
        "audio_tts": False, "osd_text": False, "osd_timestamp": True,
    },
    "D210": {
        "ptz_mode": "none", "zoom": False, "preset": False, "alarm": False,
        "alarm_trigger": False, "audio_mic": True, "audio_speaker": True,
        "audio_tts": False, "osd_text": False, "osd_timestamp": True,
    },
    "D230": {
        "ptz_mode": "none", "zoom": False, "preset": False, "alarm": False,
        "alarm_trigger": False, "audio_mic": True, "audio_speaker": True,
        "audio_tts": False, "osd_text": False, "osd_timestamp": True,
    },
    "D235": {
        "ptz_mode": "none", "zoom": False, "preset": False, "alarm": False,
        "alarm_trigger": False, "audio_mic": True, "audio_speaker": True,
        "audio_tts": False, "osd_text": False, "osd_timestamp": True,
    },
}


# Boolean-valued capability keys — used by :func:`feature_supported` and
# :func:`models_supporting`. ``ptz_mode`` is excluded because it's a tri-state
# string; callers that need it use :func:`ptz_mode_for_model` directly.
_BOOL_CAPABILITIES: frozenset[str] = frozenset(
    {
        "zoom",
        "preset",
        "alarm",
        "alarm_trigger",
        "audio_mic",
        "audio_speaker",
        "audio_tts",
        "osd_text",
        "osd_timestamp",
    }
)


def capabilities_for_model(model: str | None) -> dict[str, object]:
    """Resolve the §3.3.1 capability dict for a model string.

    Returns an empty dict for unknown models — callers should treat unknown
    as "no capabilities" and either fail closed (exit 5) or warn loudly,
    depending on context.
    """
    norm = _normalize_model(model)
    if not norm:
        return {}
    for prefix, caps in MODEL_CAPABILITIES.items():
        if norm.startswith(prefix):
            return dict(caps)
    return {}


def feature_supported(model: str | None, feature: str) -> bool:
    """``True`` iff ``model`` supports ``feature`` per the §3.3.1 matrix.

    ``feature`` is one of the boolean capability keys (see
    :data:`_BOOL_CAPABILITIES`). For PTZ, callers want :func:`ptz_mode_for_model`
    instead — ``ptz_mode`` is tri-state, not a boolean.

    Unknown models return ``False`` (fail-closed). Unknown features raise
    ``KeyError`` so a typo at the call-site is loud, not silent.
    """
    if feature not in _BOOL_CAPABILITIES:
        raise KeyError(
            f"unknown capability {feature!r}; expected one of "
            f"{sorted(_BOOL_CAPABILITIES)}"
        )
    caps = capabilities_for_model(model)
    return bool(caps.get(feature, False))


def ptz_mode_for_model(model: str | None) -> str:
    """Return ``"none"`` | ``"step"`` | ``"continuous"`` per §3.3.1.

    Unknown models return ``"none"`` (fail-closed — verbs exit 5).
    """
    caps = capabilities_for_model(model)
    mode = caps.get("ptz_mode", "none")
    if mode in {"none", "step", "continuous"}:
        return str(mode)
    return "none"


def models_supporting(feature: str) -> list[str]:
    """Sorted list of model prefixes that support ``feature``.

    Used to build the ``hint`` field on exit-5 errors so operators know
    which models actually support what they tried.
    """
    if feature == "ptz_mode":
        return sorted(
            prefix
            for prefix, caps in MODEL_CAPABILITIES.items()
            if caps.get("ptz_mode") in {"step", "continuous"}
        )
    if feature not in _BOOL_CAPABILITIES:
        raise KeyError(
            f"unknown capability {feature!r}; expected 'ptz_mode' or one of "
            f"{sorted(_BOOL_CAPABILITIES)}"
        )
    return sorted(
        prefix
        for prefix, caps in MODEL_CAPABILITIES.items()
        if caps.get(feature)
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
    "MODEL_CAPABILITIES",
    "MODEL_FEATURES",
    "VERIFIED_MODELS",
    "capabilities_for_model",
    "feature_supported",
    "features_for_model",
    "first_str",
    "flatten_basic_info",
    "format_mac",
    "model_supported",
    "models_supporting",
    "ptz_mode_for_model",
]
