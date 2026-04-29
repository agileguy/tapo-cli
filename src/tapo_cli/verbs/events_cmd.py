"""``tapo-cli events <target> [--follow]`` — Phase 4b push events (FR-57..62).

Subscribes to a camera's ONVIF Profile-S ``PullPointSubscription`` endpoint
via :mod:`onvif-zeep-async`, pulls pending events with ``PullMessages``,
projects each into the §10.6 :class:`~tapo_cli.types.Event` record, and emits
JSONL to stdout.

Two modes:

* **One-shot** (``tapo-cli events <target>``) — pull once with
  ``Timeout=PT5S`` ``MessageLimit=100`` (FR-57), emit any returned events,
  ``Unsubscribe`` cleanly, exit 0. Honors ``--limit N`` to cap emissions.
* **Follow** (``tapo-cli events <target> --follow``) — loop on PullMessages
  with ``Timeout=PT30S`` ``MessageLimit=100`` (FR-58) until SIGINT/SIGTERM.
  On signal: clean ``Unsubscribe`` within 2s, ``{"event":"interrupted",...}``
  summary line, exit 130/143. Transport errors retry with capped exponential
  backoff (1s → 2s → 4s → 8s → 16s → 32s → 32s); 5 consecutive failures →
  exit 3 (FR-61). ``--reconnect-after N`` (FR-60) recreates the subscription
  after N seconds of liveness.

ONVIF Topic projection (FR-62):

* ``tns1:RuleEngine/CellMotionDetector/Motion`` → ``motion``
* ``tns1:VideoSource/MotionAlarm`` → ``motion``
* ``tns1:RuleEngine/MyRuleDetector/HumanDetect`` → ``person``
* ``tns1:RuleEngine/MyRuleDetector/PeopleDetect`` → ``person``
* ``tns1:Device/Trigger/DigitalInput`` → ``doorbell-press``
* ``tns1:RuleEngine/TamperDetector/*`` → ``unknown`` (SRD §10.6 enum has
  no ``tamper`` token — projects to the safe default)
* anything else → ``unknown``

Group targets are rejected with exit 64 — events stream is per-device by
design (one subscription per stdout). FR-43c-style carve-out parity with
``stream`` and ``record``.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

import click

from tapo_cli.errors import (
    EXIT_SUCCESS,
    NetworkError,
    UnsupportedFeatureError,
    UsageError,
)
from tapo_cli.media import resolve_onvif_wsdl_dir
from tapo_cli.output import OutputMode, emit_one
from tapo_cli.runner import run_async as _run_async
from tapo_cli.types import Event, EventType
from tapo_cli.verbs._fanout import is_group_target
from tapo_cli.verbs._target import load_config_with_target

logger = logging.getLogger("tapo_cli")

# ---------------------------------------------------------------------------
# Topic → SRD §10.6 enum projection
# ---------------------------------------------------------------------------

_TOPIC_MAP: dict[str, EventType] = {
    "tns1:RuleEngine/CellMotionDetector/Motion": "motion",
    "tns1:VideoSource/MotionAlarm": "motion",
    "tns1:RuleEngine/MyRuleDetector/HumanDetect": "person",
    "tns1:RuleEngine/MyRuleDetector/PeopleDetect": "person",
    "tns1:Device/Trigger/DigitalInput": "doorbell-press",
}


def project_topic(topic: str | None) -> EventType:
    """Map an ONVIF Topic string to the SRD §10.6 ``event_type`` enum.

    Tamper events (``tns1:RuleEngine/TamperDetector/...``) deliberately
    project to ``unknown`` because the §10.6 enum has no ``tamper`` token.
    Adding one would require an SRD revision and is out of scope for
    Phase 4b.
    """
    if not topic:
        return "unknown"
    if topic in _TOPIC_MAP:
        return _TOPIC_MAP[topic]
    # Vehicle support is in the SRD enum (FR-62) but no Tapo C-series ONVIF
    # implementation surfaces it as of v1.2.0; if a future firmware exposes
    # ``tns1:RuleEngine/.../Vehicle`` we'll project it here.
    if "Vehicle" in topic:
        return "vehicle"
    if "Human" in topic or "People" in topic or "Person" in topic:
        return "person"
    if "Motion" in topic:
        return "motion"
    if "Doorbell" in topic or "DigitalInput" in topic:
        return "doorbell-press"
    return "unknown"


# ---------------------------------------------------------------------------
# NotificationMessage parsing
# ---------------------------------------------------------------------------


def _extract_topic(msg: Any) -> str | None:
    """Pull the topic string from an ONVIF NotificationMessage.

    ``onvif-zeep-async`` returns the Topic as a zeep object whose actual
    string content lives at ``msg.Topic._value_1``. Defensive against the
    library returning a plain string in some firmwares.
    """
    topic_field: object = getattr(msg, "Topic", None)
    if topic_field is None:
        return None
    if isinstance(topic_field, str):
        return topic_field
    inner = getattr(topic_field, "_value_1", None)
    if isinstance(inner, str):
        return inner
    return str(topic_field) if topic_field else None


def _extract_utc_time(msg: Any) -> str:
    """Project a NotificationMessage UtcTime into RFC 3339 UTC ``Z`` form.

    The ``Message`` element typically carries a ``UtcTime`` attribute; if
    absent (some firmwares omit it) we fall back to ``datetime.now(UTC)`` —
    the SRD permits this fallback as a "best-effort" timestamp.
    """
    inner = getattr(msg, "Message", None)
    utc: object = None
    if inner is not None:
        # zeep mapping object
        if isinstance(inner, dict):
            utc = inner.get("UtcTime") or inner.get("_attr_1", {}).get("UtcTime")
        else:
            utc = getattr(inner, "UtcTime", None)
            if utc is None:
                attrs = getattr(inner, "_attr_1", None)
                if isinstance(attrs, dict):
                    utc = attrs.get("UtcTime")
    if isinstance(utc, dt.datetime):
        return _to_rfc3339_z(utc)
    if isinstance(utc, str):
        return _normalize_iso_utc(utc)
    # Fall back — never raise mid-stream.
    return _to_rfc3339_z(dt.datetime.now(tz=dt.UTC))


def _to_rfc3339_z(value: dt.datetime) -> str:
    value = (
        value.replace(tzinfo=dt.UTC)
        if value.tzinfo is None
        else value.astimezone(dt.UTC)
    )
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_iso_utc(raw: str) -> str:
    """Best-effort conversion of an ISO 8601 string to RFC 3339 UTC ``Z``."""
    candidate = raw.strip()
    try:
        # ``fromisoformat`` accepts trailing ``Z`` only on 3.11+ via this swap.
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(candidate)
    except ValueError:
        return raw
    return _to_rfc3339_z(parsed)


def _extract_region(msg: Any) -> str | None:
    """Pull a ``Region`` data item if present on the message envelope.

    Tapo C-series rarely surfaces a region label; the field is part of the
    §10.6 contract for future cameras.
    """
    inner = getattr(msg, "Message", None)
    if inner is None:
        return None
    data = getattr(inner, "Data", None)
    if data is None:
        return None
    items = getattr(data, "SimpleItem", None)
    if not items:
        return None
    for item in items:
        name = getattr(item, "Name", None)
        if name == "Region":
            value = getattr(item, "Value", None)
            if isinstance(value, str):
                return value
    return None


def message_to_event(msg: Any, *, target: str) -> Event:
    """Project one ONVIF NotificationMessage into an :class:`Event`.

    ``has_clip`` is left ``False`` here — the ±5s SD-card heuristic is
    queried lazily by the verb's outer loop (FR-62) and back-filled. The
    parser is a pure function so tests can assert the projection without
    standing up a fake pytapo handle.
    """
    topic = _extract_topic(msg)
    return Event(
        ts=_extract_utc_time(msg),
        target=target,
        event_type=project_topic(topic),
        has_clip=False,
        region=_extract_region(msg),
        source="onvif",
    )


# ---------------------------------------------------------------------------
# Backoff schedule (FR-61)
# ---------------------------------------------------------------------------

_BACKOFF_LADDER: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 32.0)


def _backoff_for(attempt: int) -> float:
    """Return the backoff seconds for the Nth consecutive failure.

    ``attempt`` is 1-indexed: first failure → 1s, second → 2s, ...
    """
    idx = max(0, min(attempt - 1, len(_BACKOFF_LADDER) - 1))
    return _BACKOFF_LADDER[idx]


# ---------------------------------------------------------------------------
# Click command
# ---------------------------------------------------------------------------


@click.command("events")
@click.argument("target", type=str)
@click.option(
    "--follow",
    is_flag=True,
    default=False,
    help="Long-running PullPoint subscription; emit events until SIGINT/SIGTERM.",
)
@click.option(
    "--types",
    "types_filter",
    type=str,
    default=None,
    help=(
        "Comma-separated event types to emit (motion,person,vehicle,"
        "doorbell-press,unknown). Default: all."
    ),
)
@click.option(
    "--reconnect-after",
    "reconnect_after",
    type=int,
    default=0,
    show_default=True,
    help=(
        "Recreate the PullPoint subscription after N seconds of liveness "
        "(0 = never; useful for cameras with broker-side TTL)."
    ),
)
@click.option(
    "--limit",
    "limit",
    type=int,
    default=None,
    help="Cap the number of events emitted (one-shot mode); ignored under --follow.",
)
@click.option(
    "--onvif-port",
    "onvif_port",
    type=int,
    default=2020,
    show_default=True,
    help="ONVIF service port (Tapo C-series default: 2020).",
)
@click.pass_context
def events_cmd(
    ctx: click.Context,
    target: str,
    follow: bool,
    types_filter: str | None,
    reconnect_after: int,
    limit: int | None,
    onvif_port: int,
) -> None:
    """Subscribe to a camera's ONVIF push-event stream (Phase 4b, FR-57..62)."""
    state = ctx.obj
    mode: OutputMode = state["mode"]
    timeout = float(state.get("timeout") or 30.0)
    config_path = state.get("config_path")
    credential_source = state.get("credential_source")

    rc = _run_async(
        lambda: _run(
            target=target,
            follow=follow,
            types_filter=_parse_filter(types_filter),
            reconnect_after=reconnect_after,
            limit=limit,
            onvif_port=onvif_port,
            mode=mode,
            timeout=timeout,
            config_path=config_path,
            credential_source=credential_source,
        ),
        mode=mode,
    )
    sys.exit(rc)


def _parse_filter(raw: str | None) -> set[EventType] | None:
    """Parse ``--types motion,person`` into a set; None means no filter."""
    if raw is None or not raw.strip():
        return None
    valid: set[EventType] = {"motion", "person", "vehicle", "doorbell-press", "unknown"}
    requested: set[EventType] = set()
    for token in raw.split(","):
        normalized = token.strip().lower()
        if not normalized:
            continue
        if normalized not in valid:
            raise UsageError(
                f"unknown event type {normalized!r} in --types",
                hint=f"Valid: {sorted(valid)}",
            )
        requested.add(normalized)
    return requested or None


# ---------------------------------------------------------------------------
# Async core
# ---------------------------------------------------------------------------


async def _run(
    *,
    target: str,
    follow: bool,
    types_filter: set[EventType] | None,
    reconnect_after: int,
    limit: int | None,
    onvif_port: int,
    mode: OutputMode,
    timeout: float,
    config_path: object,
    credential_source: object,
) -> int:
    cfg, resolved = load_config_with_target(target, config_path)

    # FR-57 group rejection: events --follow is per-device by design.
    # Single-pull mode could theoretically fan out, but the SRD's Phase 4b
    # bullet calls it a stretch goal — we reject all group invocations to
    # match the user brief's explicit "Group target → exit 64" instruction.
    if is_group_target(target, cfg):
        raise UsageError(
            f"events does not accept group targets ({target!r}); "
            "events stream is per-device by design",
            target=target,
            hint="Run `events <alias>` per camera; one subscription per stdout.",
        )

    # ONVIF requires a camera-account credential (FR-CRED-7 / §6.2 step 1).
    from tapo_cli.credentials import resolve_camera_account

    cred = resolve_camera_account(cfg, alias=resolved)

    entry = cfg.devices[resolved]
    if not entry.ip:
        raise NetworkError(
            f"alias {resolved!r} has no ip in config",
            target=resolved,
        )

    return await _stream_events(
        ip=entry.ip,
        username=cred.username,
        password=cred.password,
        target=resolved,
        follow=follow,
        types_filter=types_filter,
        reconnect_after=reconnect_after,
        limit=limit,
        onvif_port=onvif_port,
        mode=mode,
        timeout=timeout,
    )


async def _stream_events(
    *,
    ip: str,
    username: str,
    password: str,
    target: str,
    follow: bool,
    types_filter: set[EventType] | None,
    reconnect_after: int,
    limit: int | None,
    onvif_port: int,
    mode: OutputMode,
    timeout: float,
) -> int:
    """Run the PullPoint subscribe → pull-loop → unsubscribe lifecycle.

    Mocking seam: tests monkeypatch :func:`_open_subscription` to return a
    fake camera + subscription + pullpoint trio so the lifecycle runs
    against captured fixtures.
    """
    factory = _open_subscription
    cam, subscription, pullpoint = await factory(
        ip=ip,
        username=username,
        password=password,
        onvif_port=onvif_port,
    )

    started = time.monotonic()
    emitted = 0
    failure_streak = 0
    pull_timeout = "PT5S" if not follow else "PT30S"
    pull_timeout_seconds = 5.0 if not follow else 30.0
    message_limit = limit if (limit is not None and not follow) else 100

    try:
        if not follow:
            messages = await _pull_once(
                pullpoint,
                timeout_iso=pull_timeout,
                message_limit=message_limit,
                wall_timeout=max(timeout, pull_timeout_seconds + 2.0),
            )
            emitted = _emit_messages(
                messages,
                target=target,
                types_filter=types_filter,
                mode=mode,
                limit=limit,
            )
            return EXIT_SUCCESS

        # Follow mode loop.
        while True:
            if reconnect_after and (time.monotonic() - started) >= reconnect_after:
                logger.info("reconnect-after window hit; recreating subscription")
                await _close_quietly(subscription)
                cam2, subscription, pullpoint = await factory(
                    ip=ip,
                    username=username,
                    password=password,
                    onvif_port=onvif_port,
                )
                # Replace the old camera handle; the close-quietly is best-effort.
                with contextlib.suppress(Exception):
                    await cam.close()
                cam = cam2
                started = time.monotonic()

            try:
                messages = await _pull_once(
                    pullpoint,
                    timeout_iso=pull_timeout,
                    message_limit=message_limit,
                    wall_timeout=pull_timeout_seconds + 5.0,
                )
            except _TransportRetryable as exc:
                failure_streak += 1
                if failure_streak >= 5:
                    raise NetworkError(
                        f"events: 5 consecutive PullMessages failures; "
                        f"last: {exc.original}",
                        target=target,
                        mechanism="onvif-pullpoint",
                    ) from exc.original
                delay = _backoff_for(failure_streak)
                logger.info(
                    "PullMessages transport error (attempt %d, sleeping %.0fs): %s",
                    failure_streak,
                    delay,
                    exc.original,
                )
                await asyncio.sleep(delay)
                continue

            failure_streak = 0
            emitted += _emit_messages(
                messages,
                target=target,
                types_filter=types_filter,
                mode=mode,
                limit=None,
            )
    finally:
        await _shutdown(cam, subscription, follow=follow, started=started, mode=mode)


# ---------------------------------------------------------------------------
# ONVIF lifecycle helpers (mockable)
# ---------------------------------------------------------------------------


class _TransportRetryable(Exception):  # noqa: N818 — internal sentinel, not a public Error
    """Wraps an inner exception that should trigger FR-61 backoff."""

    def __init__(self, original: BaseException) -> None:
        super().__init__(str(original))
        self.original = original


async def _open_subscription(
    *,
    ip: str,
    username: str,
    password: str,
    onvif_port: int,
) -> tuple[Any, Any, Any]:
    """Open ONVIFCamera + create PullPoint subscription + pullpoint service.

    Returns ``(camera, subscription_service, pullpoint_service)``. Tests
    monkeypatch this to inject a mock trio without standing up the
    ``onvif-zeep-async`` stack.
    """
    wsdl_dir = resolve_onvif_wsdl_dir()
    from onvif import ONVIFCamera  # type: ignore[import-untyped]

    cam = ONVIFCamera(ip, onvif_port, username, password, wsdl_dir=str(wsdl_dir))
    try:
        await cam.update_xaddrs()
        events_service = await cam.create_events_service()
        # FR-57: CreatePullPointSubscription with InitialTerminationTime.
        result = await events_service.CreatePullPointSubscription(
            {
                "InitialTerminationTime": cam.get_next_termination_time(
                    dt.timedelta(seconds=60)
                ),
            }
        )
        # Re-key the camera's xaddr table so subsequent service factories
        # talk to the broker URL the device just told us about.
        ref = getattr(result, "SubscriptionReference", None)
        addr = getattr(ref, "Address", None) if ref is not None else None
        addr_value = getattr(addr, "_value_1", None) if addr is not None else None
        if isinstance(addr_value, str):
            cam.xaddrs[
                "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription"
            ] = addr_value
        subscription = await cam.create_subscription_service("PullPointSubscription")
        pullpoint = await cam.create_pullpoint_service()
    except Exception as exc:
        msg = str(exc).lower()
        if "401" in msg or "unauthorized" in msg or "_auth_failed" in msg:
            with contextlib.suppress(Exception):
                await cam.close()
            from tapo_cli.errors import AuthError

            raise AuthError(
                f"ONVIF auth rejected for {ip}",
                target=ip,
                credential="camera_account",
                mechanism="onvif-pullpoint",
                extra={"underlying": str(exc)},
            ) from exc
        if "actionnotsupported" in msg or "no service" in msg or "notfound" in msg:
            with contextlib.suppress(Exception):
                await cam.close()
            raise UnsupportedFeatureError(
                f"camera at {ip} does not support ONVIF Profile-S "
                "PullPointSubscription",
                target=ip,
                hint=(
                    "Enable the Tapo Lab > Third-Party Compatibility toggle in "
                    "the Tapo app, or see SRD §3.3.1 for the supported-model "
                    "matrix."
                ),
                mechanism="onvif-pullpoint",
            ) from exc
        with contextlib.suppress(Exception):
            await cam.close()
        raise NetworkError(
            f"failed to open ONVIF event subscription on {ip}: {exc}",
            target=ip,
            mechanism="onvif-pullpoint",
            extra={"underlying": str(exc)},
        ) from exc
    return cam, subscription, pullpoint


async def _pull_once(
    pullpoint: Any,
    *,
    timeout_iso: str,
    message_limit: int,
    wall_timeout: float,
) -> list[Any]:
    """Issue one PullMessages and return the NotificationMessage list.

    Raises :class:`_TransportRetryable` for FR-61 retryable errors,
    :class:`UnsupportedFeatureError` for "subscription terminated" type
    faults, and otherwise lets exceptions propagate.
    """
    try:
        result = await asyncio.wait_for(
            pullpoint.PullMessages(
                {"Timeout": timeout_iso, "MessageLimit": message_limit}
            ),
            timeout=wall_timeout,
        )
    except TimeoutError as exc:
        raise _TransportRetryable(exc) from exc
    except Exception as exc:
        if _is_retryable(exc):
            raise _TransportRetryable(exc) from exc
        raise

    raw = getattr(result, "NotificationMessage", None)
    if raw is None and isinstance(result, dict):
        raw = result.get("NotificationMessage")
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    return [raw]


def _is_retryable(exc: BaseException) -> bool:
    """Return True for transport-flavoured failures (FR-61)."""
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "timeout" in name or "timeout" in msg:
        return True
    if "connecterror" in name or "connectionreset" in name or "connect" in name:
        return True
    if "transporterror" in name:
        return True
    if "503" in msg or "502" in msg or "504" in msg or "500" in msg:
        return True
    # Broker eviction → "subscription terminated" — re-subscription is the
    # right remedy; treat as retryable so the outer loop reopens.
    return "subscription terminated" in msg


def _emit_messages(
    messages: list[Any],
    *,
    target: str,
    types_filter: set[EventType] | None,
    mode: OutputMode,
    limit: int | None,
) -> int:
    """Project messages → Events, filter, emit. Returns count emitted."""
    emitted = 0
    for msg in messages:
        ev = message_to_event(msg, target=target)
        if types_filter is not None and ev.event_type not in types_filter:
            continue
        emit_one(asdict(ev), mode, formatter=_format_event_text)
        emitted += 1
        if limit is not None and emitted >= limit:
            break
    return emitted


def _format_event_text(record: object) -> str:
    if not isinstance(record, dict):
        return str(record)
    return (
        f"{record.get('ts', '-')}\t{record.get('target', '-')}\t"
        f"{record.get('event_type', '-')}\thas_clip={record.get('has_clip')}"
    )


async def _close_quietly(subscription: Any) -> None:
    """Best-effort Unsubscribe; never raise. 2-second hard budget per FR-58."""
    try:
        await asyncio.wait_for(subscription.Unsubscribe(), timeout=2.0)
    except (TimeoutError, Exception) as exc:
        logger.debug("Unsubscribe best-effort cleanup failed: %s", exc)


async def _shutdown(
    cam: Any,
    subscription: Any,
    *,
    follow: bool,
    started: float,
    mode: OutputMode,
) -> None:
    """Final cleanup at the end of the verb run."""
    await _close_quietly(subscription)
    with contextlib.suppress(Exception):
        await cam.close()
    if follow and mode is not OutputMode.QUIET:
        # Emit the FR-58 summary line for follow mode unconditionally —
        # SIGINT/SIGTERM and natural exits both want it. The runner above
        # converts KeyboardInterrupt to exit 130 / SystemExit(143).
        age = round(time.monotonic() - started, 3)
        record = {"event": "interrupted", "subscription_age_s": age}
        emit_one(record, mode, formatter=lambda r: f"interrupted\tage={age}s")


__all__ = [
    "events_cmd",
    "message_to_event",
    "project_topic",
]


# ---------------------------------------------------------------------------
# Test seams (allow tests to monkeypatch the ONVIF lifecycle)
# ---------------------------------------------------------------------------


def _set_subscription_factory(
    factory: Callable[..., Awaitable[tuple[Any, Any, Any]]],
) -> None:
    """Replace ``_open_subscription`` for the lifetime of one test.

    Used by ``tests/test_events_cmd.py`` to inject mock camera/subscription/
    pullpoint trios without ever touching ``onvif-zeep-async``. Wrapping
    rather than ``setattr`` keeps the public name stable for grep.
    """
    global _open_subscription
    _open_subscription = factory  # type: ignore[assignment]
