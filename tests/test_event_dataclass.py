"""Tests for the §10.6 :class:`Event` dataclass (Phase 4b FR-62)."""

from __future__ import annotations

import json
import re
from dataclasses import asdict

import pytest

from tapo_cli.types import Event

_RFC3339_Z = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"
)


def test_event_required_fields() -> None:
    """All §10.6 required fields exist on the dataclass."""
    ev = Event(
        ts="2026-04-29T19:42:11Z",
        target="office",
        event_type="motion",
        has_clip=True,
    )
    assert ev.ts == "2026-04-29T19:42:11Z"
    assert ev.target == "office"
    assert ev.event_type == "motion"
    assert ev.has_clip is True
    # Optional fields default
    assert ev.region is None
    assert ev.source == "onvif"


def test_event_source_constant_is_onvif() -> None:
    """FR-62: ``source`` is the literal ``"onvif"`` for push events."""
    ev = Event(ts="2026-04-29T19:42:11Z", target="office", event_type="motion", has_clip=False)
    assert ev.source == "onvif"


def test_event_ts_rfc3339_z_form() -> None:
    """SRD §7.2: ``ts`` is RFC 3339 UTC with literal ``Z`` suffix."""
    ev = Event(
        ts="2026-04-29T19:42:11Z",
        target="office",
        event_type="motion",
        has_clip=False,
    )
    assert _RFC3339_Z.match(ev.ts), f"unexpected ts shape: {ev.ts!r}"


def test_event_serializes_to_jsonl() -> None:
    """asdict() round-trips through json.dumps for stdout emission."""
    ev = Event(
        ts="2026-04-29T19:42:11Z",
        target="office",
        event_type="doorbell-press",
        has_clip=True,
        region="full",
    )
    text = json.dumps(asdict(ev), separators=(",", ":"), sort_keys=True)
    parsed = json.loads(text)
    assert parsed["target"] == "office"
    assert parsed["event_type"] == "doorbell-press"
    assert parsed["has_clip"] is True
    assert parsed["source"] == "onvif"
    assert parsed["region"] == "full"


def test_event_id_uniqueness_assumption() -> None:
    """Operators dedupe on (target, ts, event_type) per §10.6.

    This test pins the dedupe-tuple contract: two distinct Events that
    differ on any of those three fields must produce a different tuple
    key. The §10.6 doc commits us to this so this is constitutional.
    """
    a = Event(ts="2026-04-29T19:42:11Z", target="office", event_type="motion", has_clip=False)
    b = Event(ts="2026-04-29T19:42:11Z", target="office", event_type="motion", has_clip=True)
    c = Event(ts="2026-04-29T19:42:12Z", target="office", event_type="motion", has_clip=False)
    d = Event(ts="2026-04-29T19:42:11Z", target="back-yard", event_type="motion", has_clip=False)

    # has_clip diff alone does NOT change the dedupe key.
    assert (a.target, a.ts, a.event_type) == (b.target, b.ts, b.event_type)
    # ts and target differences DO produce distinct keys.
    assert (a.target, a.ts, a.event_type) != (c.target, c.ts, c.event_type)
    assert (a.target, a.ts, a.event_type) != (d.target, d.ts, d.event_type)


def test_event_type_enum_values() -> None:
    """All §10.6 enum values construct cleanly (typed Literal at runtime)."""
    for token in ("motion", "person", "vehicle", "doorbell-press", "unknown"):
        ev = Event(
            ts="2026-04-29T19:42:11Z",
            target="office",
            event_type=token,  # type: ignore[arg-type]
            has_clip=False,
        )
        assert ev.event_type == token


def test_event_exported_from_types_module() -> None:
    """The public types surface re-exports :class:`Event`."""
    import tapo_cli.types as types_mod

    assert "Event" in types_mod.__all__
    assert types_mod.Event is Event


@pytest.mark.parametrize(
    "ts",
    [
        "2026-01-01T00:00:00Z",
        "2026-04-29T19:42:11Z",
        "2099-12-31T23:59:59Z",
    ],
)
def test_event_ts_examples_match_pattern(ts: str) -> None:
    assert _RFC3339_Z.match(ts), f"sample {ts!r} does not match RFC 3339 Z form"
