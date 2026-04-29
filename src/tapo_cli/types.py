"""Public data model for tapo-cli (SRD §10).

Plain dataclasses, no Pydantic. ``slots=True`` to keep allocations cheap and
catch attribute typos statically. All timestamps are RFC 3339 UTC strings
with the literal ``Z`` suffix per SRD §7.2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

NightVisionMode = Literal["auto", "on", "off", "ir-only", "unknown"]
LedState = Literal["on", "off"]
EventType = Literal["motion", "person", "vehicle", "doorbell-press", "unknown"]
StreamQuality = Literal["hd", "sd"]
StreamLens = Literal["wide", "telephoto"]


@dataclass(slots=True)
class Camera:
    """Full camera record (SRD §10.1).

    Populated from one or more pytapo control-plane calls. The ``supported``
    bool reflects whether the model is on the verified-list (§3.3); other
    booleans mirror the per-camera state at the moment of the probe.
    """

    alias: str
    ip: str
    mac: str
    model: str
    hardware_version: str
    firmware_version: str
    supported: bool
    motion_enabled: bool
    privacy_enabled: bool
    led_state: LedState
    night_vision_mode: NightVisionMode
    has_camera_account: bool
    last_seen: str
    """RFC 3339 UTC timestamp of the most recent successful probe."""

    features: list[str] = field(default_factory=list)
    """Subset of ["ptz", "zoom", "audio", "tts", "alarm", "led", "privacy",
    "ir", "dual-lens", "doorbell"]. SRD §10.1."""


@dataclass(slots=True)
class Stream:
    """Stream descriptor (SRD §10.2). Emitted by ``stream`` (Phase 1c)."""

    target: str
    url: str
    lens: StreamLens
    quality: StreamQuality
    protocol: Literal["rtsp"] = "rtsp"


@dataclass(slots=True)
class MotionEvent:
    """One motion event from camera history (SRD §10.3). Phase 2."""

    ts: str
    """RFC 3339 UTC timestamp with 'Z' suffix (FR-25a, §7.2)."""

    alias: str
    event_type: EventType
    has_clip: bool
    region: str | None = None
    duration_s: float | None = None


@dataclass(slots=True)
class Event:
    """One push-emitted event from an ONVIF Profile-S PullPointSubscription
    (SRD §10.6, Phase 4b).

    Identical in shape to :class:`MotionEvent` modulo the constant
    ``source: "onvif"`` field that distinguishes push (``events --follow``)
    from pull (``motion history``, source ``"pytapo"``). Operators MAY merge
    the two JSONL streams and dedupe on ``(target, ts, event_type)``.
    """

    ts: str
    """RFC 3339 UTC string with literal 'Z' suffix; derived from the
    NotificationMessage envelope's ``UtcTime`` (FR-62, §7.2)."""

    target: str
    """Alias as resolved from the verb invocation."""

    event_type: EventType
    """SRD §10.6 closed enum: motion | person | vehicle | doorbell-press
    | unknown. Projected from the ONVIF Topic on the message envelope."""

    has_clip: bool
    """True iff a recent SD-card recording falls within ±5s of ``ts``.
    Default is False (the safe default if the camera lacks SD-card metadata
    or pytapo's ``getRecordings()`` is unavailable)."""

    region: str | None = None
    """Device-specific region label (often "full"); ``None`` when the ONVIF
    notification does not carry one."""

    source: Literal["onvif"] = "onvif"
    """Constant; FR-62 invariant — distinguishes ``events`` push from
    ``motion history`` pull."""


@dataclass(slots=True)
class Preset:
    """Saved PTZ preset (SRD §10.4). Phase 1d."""

    id: int
    name: str
    pan: float | None = None
    tilt: float | None = None
    zoom: float | None = None


@dataclass(slots=True)
class SessionMetadata:
    """One row of ``auth status`` output (SRD §10.5, FR-CRED-14).

    Reconciliation note (S8): the SRD names the booleans
    ``cloud_account``/``camera_account`` to indicate whether each credential
    family is configured for this MAC's alias. Both required fields per S8.
    """

    mac: str
    cache_path: str
    """Absolute path to ``~/.config/tapo-cli/.tokens/<mac>.json``."""

    mtime: str
    """RFC 3339 UTC string of the cache file's mtime."""

    bytes_size: int
    pytapo_version: str
    """Pytapo library version that wrote the cache (FR-CRED-9)."""

    cloud_account: bool
    """True iff a cloud-account credential is configured for the alias."""

    camera_account: bool
    """True iff a per-device camera_account_file is configured."""

    alias: str | None = None
    """Resolved alias from config; ``None`` when the MAC has no alias mapping."""

    expires_at: str | None = None
    """RFC 3339 UTC string, or ``None`` when the underlying pytapo state
    blob does not expose an expiry."""


@dataclass(frozen=True, slots=True)
class ResolvedCredential:
    """Resolved credential plus source provenance (used by wrapper layer)."""

    username: str
    password: str
    family: Literal["camera_account", "cloud_account"]
    """Which credential family this is — drives auth fallback ordering and
    the ``credential`` field on auth-error envelopes (SRD §11.2)."""

    source: str
    """Human-readable provenance label, e.g. ``"per-device:front-door"``,
    ``"~/.config/tapo-cli/credentials"``, ``"env"``. Used by ``-v`` logging."""


__all__ = [
    "Camera",
    "Event",
    "EventType",
    "LedState",
    "MotionEvent",
    "NightVisionMode",
    "Preset",
    "ResolvedCredential",
    "SessionMetadata",
    "Stream",
    "StreamLens",
    "StreamQuality",
]
