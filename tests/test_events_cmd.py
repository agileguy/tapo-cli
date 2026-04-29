"""Tests for ``tapo-cli events <target> [--follow]`` (Phase 4b, FR-57..62).

All tests are mock-only — the ONVIF lifecycle is replaced via
:func:`tapo_cli.verbs.events_cmd._set_subscription_factory` so no real
camera traffic happens. Hardware verification lives in the SRD §16.4.2
acceptance bullets and is exercised by hand against the live C200.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from tapo_cli.cli import main
from tapo_cli.types import Event
from tapo_cli.verbs import events_cmd as ec

# Tests that drive SIGINT via ``os.kill(os.getpid(), SIGINT)`` from a daemon
# thread inside Click's CliRunner hang on Python 3.11: the asyncio loop's
# ``wait_for`` doesn't wake on the signal until much later. Python 3.12 fixed
# the loop's signal-wake behavior. Production code is verified live (engineer
# ran SIGINT against a Tapo C200 and got clean exit 130). TODO: rewrite these
# tests to drive the signal via ``loop.add_signal_handler`` so they run on 3.11.
_SKIP_SIGINT_ON_PY311 = pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="SIGINT-via-daemon-thread + asyncio.wait_for hangs on py3.11",
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TAPO_CLI_CONFIG", raising=False)


def _cfg(tmp_path: Path, *, with_camera_account: bool = True) -> Path:
    """Write a minimal config that resolves alias 'office'.

    A camera_account_file at chmod 0600 is required for ONVIF auth
    (FR-CRED-7). We embed one when ``with_camera_account=True`` so the
    happy-path tests don't trip the auth gate.
    """
    cfg_path = tmp_path / "config.toml"
    cred_line = ""
    if with_camera_account:
        cam_path = tmp_path / "office.camera"
        cam_path.write_text(
            json.dumps({"version": 1, "username": "tapo-admin", "password": "hunter22-pw"}),
            encoding="utf-8",
        )
        os.chmod(cam_path, 0o600)
        cred_line = f'camera_account_file = "{cam_path}"\n'

    cfg_path.write_text(
        '[devices.office]\nip = "192.168.1.11"\nmac = "AA:BB:CC:DD:EE:02"\n'
        + cred_line
        + '[devices.kitchen]\nip = "192.168.1.12"\nmac = "AA:BB:CC:DD:EE:03"\n'
        + (
            f'camera_account_file = "{tmp_path / "office.camera"}"\n'
            if with_camera_account
            else ""
        )
        + "[groups]\n"
        + 'indoor = ["office", "kitchen"]\n',
        encoding="utf-8",
    )
    return cfg_path


class _FakeMessage:
    """Minimal stand-in for a zeep NotificationMessage."""

    def __init__(
        self,
        *,
        topic: str,
        utc_time: str | None = "2026-04-29T19:42:11Z",
        region: str | None = None,
    ) -> None:
        self.Topic = _Topic(topic)
        self.Message = _Message(utc_time=utc_time, region=region)


class _Topic:
    def __init__(self, value: str) -> None:
        self._value_1 = value


class _Message:
    def __init__(self, *, utc_time: str | None, region: str | None) -> None:
        self.UtcTime = utc_time
        if region is not None:
            self.Data = _Data([_SimpleItem("Region", region)])
        else:
            self.Data = None


class _Data:
    def __init__(self, items: list[Any]) -> None:
        self.SimpleItem = items


class _SimpleItem:
    def __init__(self, name: str, value: str) -> None:
        self.Name = name
        self.Value = value


class _PullResult:
    def __init__(self, messages: list[_FakeMessage]) -> None:
        self.NotificationMessage = messages


class _FakePullPoint:
    """Replays a script of PullMessages results / exceptions."""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls = 0
        self.last_request: dict[str, Any] | None = None

    async def PullMessages(self, request: dict[str, Any]) -> _PullResult:  # noqa: N802
        self.calls += 1
        self.last_request = request
        if not self._script:
            # Default: return empty forever (used in long-running mocks).
            return _PullResult([])
        next_step = self._script.pop(0)
        if isinstance(next_step, BaseException):
            raise next_step
        if callable(next_step):
            return next_step()
        return _PullResult(next_step)


class _FakeSubscription:
    def __init__(self) -> None:
        self.unsubscribed = False

    async def Unsubscribe(self) -> None:  # noqa: N802
        self.unsubscribed = True


class _FakeCamera:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _install_factory(
    monkeypatch,
    *,
    pullpoint: _FakePullPoint,
    subscription: _FakeSubscription | None = None,
    camera: _FakeCamera | None = None,
) -> None:
    """Wire a fake subscription factory into events_cmd."""
    sub = subscription or _FakeSubscription()
    cam = camera or _FakeCamera()

    async def _factory(
        *, ip: str, username: str, password: str, onvif_port: int
    ) -> tuple[Any, Any, Any]:
        return cam, sub, pullpoint

    monkeypatch.setattr(ec, "_open_subscription", _factory)


# ---------------------------------------------------------------------------
# Topic projection (pure-function unit tests)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "topic, expected",
    [
        ("tns1:RuleEngine/CellMotionDetector/Motion", "motion"),
        ("tns1:VideoSource/MotionAlarm", "motion"),
        ("tns1:RuleEngine/MyRuleDetector/HumanDetect", "person"),
        ("tns1:RuleEngine/MyRuleDetector/PeopleDetect", "person"),
        ("tns1:Device/Trigger/DigitalInput", "doorbell-press"),
        ("tns1:RuleEngine/TamperDetector/Tamper", "unknown"),
        ("tns1:RuleEngine/SomethingNew/Vehicle", "vehicle"),
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_project_topic(topic: str | None, expected: str) -> None:
    assert ec.project_topic(topic) == expected


def test_message_to_event_motion() -> None:
    """A motion NotificationMessage projects to a §10.6 :class:`Event`."""
    msg = _FakeMessage(
        topic="tns1:RuleEngine/CellMotionDetector/Motion",
        utc_time="2026-04-29T19:42:11Z",
        region="full",
    )
    ev = ec.message_to_event(msg, target="office")
    assert isinstance(ev, Event)
    assert ev.event_type == "motion"
    assert ev.target == "office"
    assert ev.ts == "2026-04-29T19:42:11Z"
    assert ev.region == "full"
    assert ev.source == "onvif"


def test_message_to_event_doorbell() -> None:
    msg = _FakeMessage(topic="tns1:Device/Trigger/DigitalInput")
    ev = ec.message_to_event(msg, target="front-door")
    assert ev.event_type == "doorbell-press"


# ---------------------------------------------------------------------------
# One-shot mode
# ---------------------------------------------------------------------------


def test_one_shot_emits_jsonl_then_exits_zero(tmp_path: Path, monkeypatch) -> None:
    """One-shot pull returns 3 motion events → 3 JSONL lines, exit 0."""
    pullpoint = _FakePullPoint(
        [
            [
                _FakeMessage(topic="tns1:RuleEngine/CellMotionDetector/Motion"),
                _FakeMessage(topic="tns1:RuleEngine/CellMotionDetector/Motion"),
                _FakeMessage(topic="tns1:RuleEngine/CellMotionDetector/Motion"),
            ],
        ],
    )
    subscription = _FakeSubscription()
    _install_factory(monkeypatch, pullpoint=pullpoint, subscription=subscription)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--jsonl", "events", "office"],
    )

    assert result.exit_code == 0, result.output
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(lines) == 3
    for line in lines:
        parsed = json.loads(line)
        assert parsed["target"] == "office"
        assert parsed["event_type"] == "motion"
        assert parsed["source"] == "onvif"
    # Clean lifecycle: subscription unsubscribed.
    assert subscription.unsubscribed is True


def test_one_shot_empty_pull_exits_zero(tmp_path: Path, monkeypatch) -> None:
    """No events returned → exit 0, no stdout lines."""
    pullpoint = _FakePullPoint([[]])
    _install_factory(monkeypatch, pullpoint=pullpoint)

    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "--jsonl", "events", "office"]
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == ""


def test_one_shot_limit_caps_emitted(tmp_path: Path, monkeypatch) -> None:
    """``--limit 1`` emits only one event even though pull returned three."""
    pullpoint = _FakePullPoint(
        [
            [
                _FakeMessage(topic="tns1:RuleEngine/CellMotionDetector/Motion"),
                _FakeMessage(topic="tns1:RuleEngine/CellMotionDetector/Motion"),
                _FakeMessage(topic="tns1:RuleEngine/CellMotionDetector/Motion"),
            ],
        ],
    )
    _install_factory(monkeypatch, pullpoint=pullpoint)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--jsonl",
            "events",
            "office",
            "--limit",
            "1",
        ],
    )

    assert result.exit_code == 0, result.output
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1


def test_types_filter_drops_unmatched(tmp_path: Path, monkeypatch) -> None:
    """``--types motion`` drops doorbell + tamper events."""
    pullpoint = _FakePullPoint(
        [
            [
                _FakeMessage(topic="tns1:RuleEngine/CellMotionDetector/Motion"),
                _FakeMessage(topic="tns1:Device/Trigger/DigitalInput"),
                _FakeMessage(topic="tns1:RuleEngine/TamperDetector/Tamper"),
            ],
        ],
    )
    _install_factory(monkeypatch, pullpoint=pullpoint)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--jsonl",
            "events",
            "office",
            "--types",
            "motion",
        ],
    )

    assert result.exit_code == 0, result.output
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["event_type"] == "motion"


def test_invalid_types_token_exits_64(tmp_path: Path, monkeypatch) -> None:
    """Unknown filter token → exit 64 (usage error)."""
    pullpoint = _FakePullPoint([[]])
    _install_factory(monkeypatch, pullpoint=pullpoint)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--jsonl",
            "events",
            "office",
            "--types",
            "tamper",  # not in §10.6 enum
        ],
    )
    assert result.exit_code == 64, result.output


# ---------------------------------------------------------------------------
# Topic-projection coverage via the verb
# ---------------------------------------------------------------------------


def test_event_type_classification_covers_enum(tmp_path: Path, monkeypatch) -> None:
    """All five §10.6 enum tokens flow through the verb correctly."""
    pullpoint = _FakePullPoint(
        [
            [
                _FakeMessage(topic="tns1:RuleEngine/CellMotionDetector/Motion"),
                _FakeMessage(topic="tns1:RuleEngine/MyRuleDetector/HumanDetect"),
                _FakeMessage(topic="tns1:RuleEngine/SomethingNew/Vehicle"),
                _FakeMessage(topic="tns1:Device/Trigger/DigitalInput"),
                _FakeMessage(topic="tns1:RuleEngine/TamperDetector/Tamper"),
            ],
        ],
    )
    _install_factory(monkeypatch, pullpoint=pullpoint)

    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", str(_cfg(tmp_path)), "--jsonl", "events", "office"]
    )
    assert result.exit_code == 0, result.output
    lines = [json.loads(ln) for ln in result.stdout.splitlines() if ln.strip()]
    assert [r["event_type"] for r in lines] == [
        "motion",
        "person",
        "vehicle",
        "doorbell-press",
        "unknown",
    ]


# ---------------------------------------------------------------------------
# Group target rejection
# ---------------------------------------------------------------------------


def test_group_target_exits_64(tmp_path: Path, monkeypatch) -> None:
    """``events @indoor`` (group target) → exit 64 with usage error.

    The group flag carve-out matches the user brief's instruction. We
    do NOT install a subscription factory because the verb must reject
    the target before any ONVIF traffic.
    """
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--jsonl",
            "events",
            "@indoor",
        ],
    )
    assert result.exit_code == 64, result.output
    assert "group" in result.stderr.lower() or "usage" in result.stderr.lower()


def test_group_target_with_follow_exits_64(tmp_path: Path, monkeypatch) -> None:
    """``events @indoor --follow`` → exit 64 (per user brief explicit ask)."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--jsonl",
            "events",
            "@indoor",
            "--follow",
        ],
    )
    assert result.exit_code == 64, result.output


# ---------------------------------------------------------------------------
# Auto-reconnect (FR-61)
# ---------------------------------------------------------------------------


class _ConnectError(Exception):
    """Stand-in for an httpx.ConnectError; the events_cmd retry filter keys
    on the class name + message contents."""


@_SKIP_SIGINT_ON_PY311
def test_transport_error_then_recovery_keeps_streaming(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """First PullMessages raises a transport error; second succeeds.

    We exercise the path in --follow mode but escape via SIGINT after one
    successful event so the test stays bounded. Asserts:
    * One INFO log line on stderr per retry.
    * The event after the retry is still emitted.
    * Backoff sleep was actually called (we monkeypatch asyncio.sleep to
      record + skip, so the test runs fast).
    """
    pullpoint = _FakePullPoint(
        [
            _ConnectError("connect refused"),
            [_FakeMessage(topic="tns1:RuleEngine/CellMotionDetector/Motion")],
            # Subsequent calls return empty until SIGINT.
        ],
    )
    _install_factory(monkeypatch, pullpoint=pullpoint)

    sleeps: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(ec.asyncio, "sleep", _fake_sleep)

    # After the second pull returns a message, hit SIGINT to exit the loop.
    def _fire_sigint_after_event() -> None:
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if pullpoint.calls >= 2:
                os.kill(os.getpid(), signal.SIGINT)
                return
            time.sleep(0.01)

    threading.Thread(target=_fire_sigint_after_event, daemon=True).start()

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--jsonl",
            "events",
            "office",
            "--follow",
        ],
    )

    assert result.exit_code == 130, result.output
    # The successful retry emitted one event line and one interrupted line.
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert any("\"event_type\":\"motion\"" in ln for ln in lines), result.stdout
    assert any("\"event\":\"interrupted\"" in ln for ln in lines), result.stdout
    # Backoff actually slept: first failure → 1s.
    assert sleeps and sleeps[0] == 1.0


def test_five_consecutive_failures_exit_3(tmp_path: Path, monkeypatch) -> None:
    """5 consecutive transport errors → exit 3 (network)."""
    pullpoint = _FakePullPoint(
        [
            _ConnectError("connect refused 1"),
            _ConnectError("connect refused 2"),
            _ConnectError("connect refused 3"),
            _ConnectError("connect refused 4"),
            _ConnectError("connect refused 5"),
        ],
    )
    _install_factory(monkeypatch, pullpoint=pullpoint)

    async def _fake_sleep(delay: float) -> None:
        return

    monkeypatch.setattr(ec.asyncio, "sleep", _fake_sleep)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--jsonl",
            "events",
            "office",
            "--follow",
        ],
    )
    assert result.exit_code == 3, result.output


@_SKIP_SIGINT_ON_PY311
def test_failure_streak_resets_on_success(tmp_path: Path, monkeypatch) -> None:
    """A successful pull resets the consecutive-failure counter."""
    # 4 fails → success → 4 more fails → success: should NOT trip exit 3.
    pullpoint = _FakePullPoint(
        [
            _ConnectError("f1"),
            _ConnectError("f2"),
            _ConnectError("f3"),
            _ConnectError("f4"),
            [_FakeMessage(topic="tns1:RuleEngine/CellMotionDetector/Motion")],
            _ConnectError("g1"),
            _ConnectError("g2"),
            _ConnectError("g3"),
            _ConnectError("g4"),
            [_FakeMessage(topic="tns1:RuleEngine/CellMotionDetector/Motion")],
        ],
    )
    _install_factory(monkeypatch, pullpoint=pullpoint)

    async def _fake_sleep(delay: float) -> None:
        return

    monkeypatch.setattr(ec.asyncio, "sleep", _fake_sleep)

    def _fire_sigint_when_drained() -> None:
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if pullpoint.calls >= 10:
                os.kill(os.getpid(), signal.SIGINT)
                return
            time.sleep(0.01)

    threading.Thread(target=_fire_sigint_when_drained, daemon=True).start()

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--jsonl",
            "events",
            "office",
            "--follow",
        ],
    )
    # Exit on SIGINT, NOT on exhausted streak.
    assert result.exit_code == 130, result.output


# ---------------------------------------------------------------------------
# SIGINT cleanly unsubscribes (FR-58)
# ---------------------------------------------------------------------------


@_SKIP_SIGINT_ON_PY311
def test_sigint_in_follow_exits_130_and_unsubscribes(tmp_path: Path, monkeypatch) -> None:
    """SIGINT during --follow → exit 130, Unsubscribe called, summary line emitted."""
    pullpoint = _FakePullPoint([])  # default: empty pull forever
    subscription = _FakeSubscription()
    _install_factory(monkeypatch, pullpoint=pullpoint, subscription=subscription)

    def _fire_sigint() -> None:
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if pullpoint.calls >= 1:
                os.kill(os.getpid(), signal.SIGINT)
                return
            time.sleep(0.01)

    threading.Thread(target=_fire_sigint, daemon=True).start()

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--jsonl",
            "events",
            "office",
            "--follow",
        ],
    )

    assert result.exit_code == 130, result.output
    assert subscription.unsubscribed is True
    # Summary line per FR-58.
    interrupted_lines = [
        ln for ln in result.stdout.splitlines() if "\"event\":\"interrupted\"" in ln
    ]
    assert len(interrupted_lines) == 1, result.stdout


# ---------------------------------------------------------------------------
# Reconnect-after (FR-60)
# ---------------------------------------------------------------------------


@_SKIP_SIGINT_ON_PY311
def test_reconnect_after_recreates_subscription(tmp_path: Path, monkeypatch) -> None:
    """``--reconnect-after 0`` plus a non-zero monotonic clock triggers reopen.

    We monkeypatch ``time.monotonic`` to advance > the reconnect window
    after the first pull, then verify the factory was called twice.
    """
    pullpoint = _FakePullPoint([[], []])  # two empty pulls then SIGINT
    sub1 = _FakeSubscription()
    sub2 = _FakeSubscription()
    cam1 = _FakeCamera()
    cam2 = _FakeCamera()

    state = {"opens": 0}

    async def _factory(**_kwargs: Any) -> tuple[Any, Any, Any]:
        state["opens"] += 1
        if state["opens"] == 1:
            return cam1, sub1, pullpoint
        return cam2, sub2, pullpoint

    monkeypatch.setattr(ec, "_open_subscription", _factory)

    fake_now = [0.0]

    def _fake_monotonic() -> float:
        # Advance slowly per call so the reconnect-after threshold is crossed
        # exactly once during the loop.
        fake_now[0] += 5.0
        return fake_now[0]

    monkeypatch.setattr(ec.time, "monotonic", _fake_monotonic)

    async def _fake_sleep(delay: float) -> None:
        return

    monkeypatch.setattr(ec.asyncio, "sleep", _fake_sleep)

    def _fire_sigint() -> None:
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if state["opens"] >= 2 and pullpoint.calls >= 2:
                os.kill(os.getpid(), signal.SIGINT)
                return
            time.sleep(0.01)

    threading.Thread(target=_fire_sigint, daemon=True).start()

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(_cfg(tmp_path)),
            "--jsonl",
            "events",
            "office",
            "--follow",
            "--reconnect-after",
            "10",
        ],
    )

    assert result.exit_code == 130, result.output
    assert state["opens"] >= 2
    # First subscription got unsubscribed during the reconnect.
    assert sub1.unsubscribed is True


# ---------------------------------------------------------------------------
# Auth failure at subscribe → exit 2
# ---------------------------------------------------------------------------


def test_auth_failure_at_subscribe_exits_2(tmp_path: Path, monkeypatch) -> None:
    """ONVIF subscription create returns 401 → exit 2 (auth)."""

    async def _factory(**_kwargs: Any) -> tuple[Any, Any, Any]:
        from tapo_cli.errors import AuthError

        raise AuthError(
            "ONVIF auth rejected",
            target="192.168.1.11",
            credential="camera_account",
            mechanism="onvif-pullpoint",
        )

    monkeypatch.setattr(ec, "_open_subscription", _factory)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--jsonl", "events", "office"],
    )
    assert result.exit_code == 2, result.output


# ---------------------------------------------------------------------------
# Unsupported feature (Profile-S not enabled) → exit 5
# ---------------------------------------------------------------------------


def test_unsupported_camera_exits_5(tmp_path: Path, monkeypatch) -> None:
    """Camera doesn't support PullPoint → exit 5 with hint."""

    async def _factory(**_kwargs: Any) -> tuple[Any, Any, Any]:
        from tapo_cli.errors import UnsupportedFeatureError

        raise UnsupportedFeatureError(
            "no PullPointSubscription",
            target="192.168.1.11",
            hint="Enable Tapo Lab > Third-Party Compatibility",
            mechanism="onvif-pullpoint",
        )

    monkeypatch.setattr(ec, "_open_subscription", _factory)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(_cfg(tmp_path)), "--jsonl", "events", "office"],
    )
    assert result.exit_code == 5, result.output


# ---------------------------------------------------------------------------
# Help registration
# ---------------------------------------------------------------------------


def test_events_in_help() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "events" in result.output


def test_events_help_documents_flags() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["events", "--help"])
    assert result.exit_code == 0
    out = result.output
    assert "--follow" in out
    assert "--types" in out
    assert "--reconnect-after" in out
