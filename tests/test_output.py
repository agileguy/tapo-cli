"""Tests for the output formatter (SRD §5.17, §7.2, §11.2)."""

from __future__ import annotations

import io
import json
import re

import pytest

from tapo_cli.errors import StructuredError
from tapo_cli.output import (
    OutputMode,
    detect_mode,
    emit_error,
    emit_stream,
    epoch_to_rfc3339,
    sort_records,
    utc_now_rfc3339,
)

# ---------------------------------------------------------------------------
# Mode detection (FR-46)
# ---------------------------------------------------------------------------


class _NoTty:
    """A stream that is NOT a tty — simulates pipes AND file redirects."""

    def __init__(self) -> None:
        self.buf = io.StringIO()

    def write(self, s: str) -> int:
        return self.buf.write(s)

    def isatty(self) -> bool:
        return False


class _IsTty:
    def __init__(self) -> None:
        self.buf = io.StringIO()

    def write(self, s: str) -> int:
        return self.buf.write(s)

    def isatty(self) -> bool:
        return True


def test_auto_mode_emits_jsonl_when_stdout_not_a_tty() -> None:
    s = _NoTty()
    mode = detect_mode(json_flag=False, jsonl_flag=False, quiet=False, stream=s)
    assert mode is OutputMode.JSONL


def test_auto_mode_emits_text_when_stdout_is_a_tty() -> None:
    s = _IsTty()
    mode = detect_mode(json_flag=False, jsonl_flag=False, quiet=False, stream=s)
    assert mode is OutputMode.TEXT


def test_quiet_overrides_everything() -> None:
    mode = detect_mode(json_flag=True, jsonl_flag=True, quiet=True, stream=_IsTty())
    assert mode is OutputMode.QUIET


def test_json_flag_takes_precedence_over_jsonl() -> None:
    mode = detect_mode(json_flag=True, jsonl_flag=True, quiet=False, stream=_NoTty())
    assert mode is OutputMode.JSON


def test_isatty_raising_treated_as_non_tty() -> None:
    """Some streams raise from isatty() — treat as non-tty (pipe/redirect)."""

    class Raises:
        def write(self, s: str) -> int:
            return len(s)

        def isatty(self) -> bool:
            raise OSError("nope")

    mode = detect_mode(json_flag=False, jsonl_flag=False, quiet=False, stream=Raises())
    assert mode is OutputMode.JSONL


# ---------------------------------------------------------------------------
# Quiet does NOT silence binary stdout payloads (S15 carve-out)
# ---------------------------------------------------------------------------


def test_quiet_mode_writes_nothing_for_text_emit() -> None:
    s = _NoTty()
    emit_stream([{"alias": "x"}], OutputMode.QUIET, formatter=str, stream=s)
    assert s.buf.getvalue() == ""


def test_quiet_carveout_documented_in_module_docstring() -> None:
    """S15 / FR-11d: snapshot --output - writes JPEG bytes despite --quiet.

    This module's QUIET mode is text-only — verbs that write binary stdout
    payloads (snapshot --output -) bypass it. We can't unit-test the verb
    here (it doesn't exist yet) but the module docstring records the
    contract so reviewers see it.
    """
    from tapo_cli import output

    assert "S15" in (output.__doc__ or "")
    assert "binary stdout" in (output.__doc__ or "")


# ---------------------------------------------------------------------------
# Timestamps RFC 3339 UTC ('Z' suffix) — SRD §7.2
# ---------------------------------------------------------------------------


_RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def test_utc_now_rfc3339_has_z_suffix() -> None:
    s = utc_now_rfc3339()
    assert _RFC3339_RE.match(s), s
    assert s.endswith("Z")
    assert "+" not in s


def test_epoch_to_rfc3339_with_known_value() -> None:
    # 2026-01-01T00:00:00Z is epoch 1767225600
    s = epoch_to_rfc3339(1767225600.0)
    assert s == "2026-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# Multi-record sort (SRD §7.2)
# ---------------------------------------------------------------------------


def test_sort_by_target_in_config_order() -> None:
    """Default sort: by ``target`` ascending in the resolved-config order."""
    records = [
        {"target": "backyard", "ts": "2026-04-01T00:00:00Z"},
        {"target": "front-door", "ts": "2026-04-01T00:00:00Z"},
        {"target": "office-cam", "ts": "2026-04-01T00:00:00Z"},
    ]
    out = sort_records(records, target_order=["front-door", "backyard", "office-cam"])
    assert [r["target"] for r in out] == ["front-door", "backyard", "office-cam"]


def test_sort_ties_broken_by_event_timestamp() -> None:
    records = [
        {"target": "cam", "ts": "2026-04-01T03:00:00Z"},
        {"target": "cam", "ts": "2026-04-01T01:00:00Z"},
        {"target": "cam", "ts": "2026-04-01T02:00:00Z"},
    ]
    out = sort_records(records, target_order=["cam"])
    assert [r["ts"] for r in out] == [
        "2026-04-01T01:00:00Z",
        "2026-04-01T02:00:00Z",
        "2026-04-01T03:00:00Z",
    ]


def test_sort_unknown_targets_go_last_alphabetically() -> None:
    records = [
        {"target": "zeta", "ts": "2026-04-01T00:00:00Z"},
        {"target": "front-door", "ts": "2026-04-01T00:00:00Z"},
        {"target": "alpha", "ts": "2026-04-01T00:00:00Z"},
    ]
    out = sort_records(records, target_order=["front-door"])
    assert [r["target"] for r in out] == ["front-door", "alpha", "zeta"]


# ---------------------------------------------------------------------------
# JSON output round-trip stability (FR-35a)
# ---------------------------------------------------------------------------


def test_emit_stream_jsonl_lines_are_valid_json() -> None:
    s = _NoTty()
    items = [{"a": 1}, {"a": 2, "b": [1, 2]}, {"a": 3}]
    emit_stream(items, OutputMode.JSONL, formatter=str, stream=s)
    lines = [line for line in s.buf.getvalue().splitlines() if line]
    assert len(lines) == 3
    for line in lines:
        json.loads(line)  # round-trip


def test_emit_stream_json_emits_single_array() -> None:
    s = _NoTty()
    items = [{"a": 1}, {"a": 2}]
    emit_stream(items, OutputMode.JSON, formatter=str, stream=s)
    payload = json.loads(s.buf.getvalue())
    assert payload == [{"a": 1}, {"a": 2}]


def test_emit_error_always_json_to_stderr() -> None:
    err = StructuredError(
        error="auth_failed",
        exit_code=2,
        target="x",
        message="m",
        hint="h",
    )
    s = _NoTty()
    emit_error(err, OutputMode.TEXT, stream=s)
    parsed = json.loads(s.buf.getvalue().rstrip())
    assert parsed["error"] == "auth_failed"
    assert parsed["exit_code"] == 2


def test_emit_error_not_silenced_by_quiet_mode() -> None:
    """SRD §11.2: --quiet does NOT silence structured errors on stderr."""
    err = StructuredError(error="device_error", exit_code=1, message="boom")
    s = _NoTty()
    emit_error(err, OutputMode.QUIET, stream=s)
    assert s.buf.getvalue().strip(), "QUIET must still emit the structured error"
    parsed = json.loads(s.buf.getvalue().rstrip())
    assert parsed["error"] == "device_error"


# ---------------------------------------------------------------------------
# Dataclass serialization
# ---------------------------------------------------------------------------


def test_emit_stream_serializes_dataclasses() -> None:
    from tapo_cli.types import SessionMetadata

    rec = SessionMetadata(
        mac="AA:BB:CC:DD:EE:01",
        cache_path="/tmp/x.json",
        mtime="2026-01-01T00:00:00Z",
        bytes_size=42,
        pytapo_version="9.9",
        cloud_account=True,
        camera_account=False,
        alias="cam",
    )
    s = _NoTty()
    emit_stream([rec], OutputMode.JSONL, formatter=str, stream=s)
    line = s.buf.getvalue().rstrip()
    parsed = json.loads(line)
    assert parsed["mac"] == "AA:BB:CC:DD:EE:01"
    assert parsed["pytapo_version"] == "9.9"


# ---------------------------------------------------------------------------
# Sanity guards for malformed JSON paths
# ---------------------------------------------------------------------------


def test_safe_dumps_round_trips_unicode_payloads() -> None:
    s = _NoTty()
    emit_stream([{"alias": "café-cam", "name": "Über"}], OutputMode.JSONL, formatter=str, stream=s)
    parsed = json.loads(s.buf.getvalue().rstrip())
    assert parsed == {"alias": "café-cam", "name": "Über"}


@pytest.mark.parametrize("mode", [OutputMode.JSON, OutputMode.JSONL])
def test_emit_stream_with_empty_iterable(mode: OutputMode) -> None:
    s = _NoTty()
    emit_stream([], mode, formatter=str, stream=s)
    out = s.buf.getvalue()
    if mode is OutputMode.JSON:
        assert json.loads(out) == []
    else:
        assert out == ""
