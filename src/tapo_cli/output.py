"""Output formatting and emission for tapo-cli (SRD §5.17, §11.2).

Four output modes (:class:`OutputMode`):

* ``TEXT`` — fixed-width human-readable on tty (default when stdout is a
  tty and neither ``--json`` nor ``--jsonl`` is set).
* ``JSON`` — pretty multi-line JSON with ``indent=2``.
* ``JSONL`` — one JSON object per line, no trailing whitespace; default
  when stdout is NOT a tty (FR-46 — file redirects, pipes, both).
* ``QUIET`` — suppresses stdout for non-binary verbs.

S15 carve-out: ``--quiet`` does NOT suppress binary stdout payloads — e.g.
a future ``snapshot --output -`` writes JPEG bytes to stdout regardless of
``--quiet`` (FR-11d). That's the verb's responsibility; this module's
``QUIET`` mode is text-only.

Strict invariant: in ``JSON`` and ``JSONL`` modes, every byte written to
stdout MUST round-trip through ``json.loads``. We validate before writing
so a programming error here cannot ever produce malformed JSON.

All emitted timestamps are RFC 3339 UTC with the literal ``Z`` suffix per
SRD §7.2. Helpers :func:`utc_now_rfc3339` / :func:`epoch_to_rfc3339` are
provided so verb modules don't have to roll their own.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from collections.abc import Callable, Iterable
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any, TextIO

from tapo_cli.errors import StructuredError


class OutputMode(Enum):
    """Output rendering mode for stdout."""

    TEXT = "text"
    JSON = "json"
    JSONL = "jsonl"
    QUIET = "quiet"


# --- Mode detection -----------------------------------------------------------


def detect_mode(
    *,
    json_flag: bool,
    jsonl_flag: bool,
    quiet: bool,
    stream: TextIO | None = None,
) -> OutputMode:
    """Resolve flags + tty state into a single :class:`OutputMode`.

    Precedence: ``--quiet`` > ``--json`` > ``--jsonl`` > tty/pipe heuristic.

    FR-46: when stdout is NOT a tty (any non-tty — pipe AND file redirect),
    auto mode picks JSONL. Tests use ``StringIO`` which has no ``isatty``
    or returns False, so the same path covers them.
    """
    if quiet:
        return OutputMode.QUIET
    if json_flag:
        return OutputMode.JSON
    if jsonl_flag:
        return OutputMode.JSONL
    s = stream if stream is not None else sys.stdout
    isatty = getattr(s, "isatty", None)
    if callable(isatty):
        try:
            if isatty():
                return OutputMode.TEXT
        except (OSError, ValueError):
            # Some streams raise on isatty() — treat as non-tty.
            pass
    return OutputMode.JSONL


# --- Timestamp helpers (SRD §7.2) ---------------------------------------------


def utc_now_rfc3339() -> str:
    """Return ``datetime.now(UTC)`` as an RFC 3339 string with ``Z`` suffix.

    Seconds resolution. Use this anywhere the SRD says "RFC 3339 UTC".
    """
    return (
        dt.datetime.now(tz=dt.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def epoch_to_rfc3339(epoch: float) -> str:
    """Convert a UNIX epoch float to RFC 3339 UTC with ``Z`` suffix."""
    return (
        dt.datetime.fromtimestamp(epoch, tz=dt.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# --- Serialization helpers ----------------------------------------------------


def _to_jsonable(item: object) -> Any:
    """Convert dataclass / mapping / scalar into a JSON-safe Python value."""
    if is_dataclass(item) and not isinstance(item, type):
        return _to_jsonable(asdict(item))
    if isinstance(item, dict):
        return {str(k): _to_jsonable(v) for k, v in item.items()}
    if isinstance(item, (list, tuple)):
        return [_to_jsonable(v) for v in item]
    if isinstance(item, (str, int, float, bool)) or item is None:
        return item
    return str(item)


def _safe_dumps(payload: object, *, pretty: bool) -> str:
    """Dump and round-trip-validate. Returns the validated string."""
    jsonable = _to_jsonable(payload)
    if pretty:
        text = json.dumps(jsonable, indent=2, sort_keys=True)
    else:
        text = json.dumps(jsonable, separators=(",", ":"), sort_keys=True)
    json.loads(text)  # belt-and-suspenders: never spew malformed JSON
    return text


# --- Sort helpers (SRD §7.2 — multi-record output sort) ----------------------


def sort_records(
    items: Iterable[Any],
    *,
    target_order: list[str] | None = None,
    target_key: str = "target",
    ts_key: str = "ts",
) -> list[Any]:
    """Sort multi-record output deterministically per §7.2.

    Default: sort by ``target`` ascending in *config order* (the order
    aliases appear in the resolved config), with ties broken by event
    timestamp ascending. Records that aren't in ``target_order`` sort after
    those that are, alphabetically.

    Args:
        items: Records (dicts or dataclasses) to sort.
        target_order: Resolved-config alias order. ``None`` falls back to
            alphabetical target order.
        target_key: Field name carrying the target alias.
        ts_key: Field name carrying the RFC 3339 timestamp tiebreaker.
    """
    materialized = list(items)

    def _get(rec: Any, key: str) -> Any:
        if isinstance(rec, dict):
            return rec.get(key)
        return getattr(rec, key, None)

    order_index: dict[str, int] = (
        {alias: i for i, alias in enumerate(target_order)} if target_order else {}
    )

    def _key(rec: Any) -> tuple[int, str, str]:
        target = str(_get(rec, target_key) or "")
        # Records with a known config-order index sort first (group 0); unknown
        # aliases sort after (group 1, alphabetical).
        idx = order_index.get(target)
        bucket = (0, idx) if idx is not None else (1, 0)
        ts = str(_get(rec, ts_key) or "")
        # Pack into a uniform tuple shape.
        return (bucket[0], target if bucket[0] == 1 else "", ts) if False else (
            bucket[0] * 1000000 + (bucket[1] if bucket[1] is not None else 0),
            target,
            ts,
        )

    return sorted(materialized, key=_key)


# --- Emission -----------------------------------------------------------------


def emit(
    item: object,
    mode: OutputMode,
    *,
    formatter: Callable[[object], str],
    stream: TextIO | None = None,
) -> None:
    """Emit a single record in the requested mode."""
    s = stream if stream is not None else sys.stdout
    if mode is OutputMode.QUIET:
        return
    if mode is OutputMode.TEXT:
        s.write(formatter(item))
        s.write("\n")
        return
    if mode is OutputMode.JSON:
        s.write(_safe_dumps(item, pretty=True))
        s.write("\n")
        return
    s.write(_safe_dumps(item, pretty=False))
    s.write("\n")


def emit_one(
    item: object,
    mode: OutputMode,
    *,
    formatter: Callable[[object], str],
    stream: TextIO | None = None,
) -> None:
    """Emit a single streaming record AND flush. Same shape as :func:`emit`
    but with explicit ``flush()`` for live consumers.
    """
    s = stream if stream is not None else sys.stdout
    if mode is OutputMode.QUIET:
        return
    if mode is OutputMode.TEXT:
        s.write(formatter(item))
        s.write("\n")
        s.flush()
        return
    if mode is OutputMode.JSON:
        s.write(_safe_dumps(item, pretty=True))
        s.write("\n")
        s.flush()
        return
    s.write(_safe_dumps(item, pretty=False))
    s.write("\n")
    s.flush()


def emit_stream(
    items: Iterable[object],
    mode: OutputMode,
    *,
    formatter: Callable[[object], str],
    stream: TextIO | None = None,
) -> None:
    """Emit a stream of records.

    In ``JSON`` mode we collect into a single array and emit pretty-printed
    once. In ``JSONL`` mode each item gets its own validated line. ``TEXT``
    delegates each item to ``formatter``. ``QUIET`` writes nothing.
    """
    s = stream if stream is not None else sys.stdout
    if mode is OutputMode.QUIET:
        return
    if mode is OutputMode.JSON:
        materialized = list(items)
        s.write(_safe_dumps(materialized, pretty=True))
        s.write("\n")
        return
    if mode is OutputMode.JSONL:
        for item in items:
            s.write(_safe_dumps(item, pretty=False))
            s.write("\n")
        return
    for item in items:
        s.write(formatter(item))
        s.write("\n")


def emit_error(
    err: StructuredError,
    mode: OutputMode,
    *,
    stream: TextIO | None = None,
) -> None:
    """Emit a :class:`StructuredError` to stderr (SRD §11.2).

    Always JSON regardless of ``mode``. ``--quiet`` does NOT suppress
    structured errors — operators still need the failure-reason envelope.
    """
    del mode  # all modes emit the same JSON envelope
    s = stream if stream is not None else sys.stderr
    text = err.to_json()
    json.loads(text)  # never spew malformed JSON
    s.write(text)
    s.write("\n")


__all__ = [
    "OutputMode",
    "detect_mode",
    "emit",
    "emit_error",
    "emit_one",
    "emit_stream",
    "epoch_to_rfc3339",
    "sort_records",
    "utc_now_rfc3339",
]
