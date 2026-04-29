"""Exit codes and structured-error model for tapo-cli (SRD §11).

Single source of truth for the integer exit codes (§11.1) and the
:class:`StructuredError` shape emitted on stderr (§11.2). Every CLI failure
path raises a subclass of :class:`TapoCliError`; the dispatcher in
:mod:`tapo_cli.cli` maps the exception to the right exit code and prints the
structured form.

The ``error`` enum strings live alongside the exit codes so tests can assert
key stability without touching CLI code.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Final

# ---------------------------------------------------------------------------
# Exit code constants (SRD §11.1)
# ---------------------------------------------------------------------------

EXIT_SUCCESS: Final[int] = 0
EXIT_DEVICE_ERROR: Final[int] = 1
EXIT_AUTH_ERROR: Final[int] = 2
EXIT_NETWORK_ERROR: Final[int] = 3
EXIT_NOT_FOUND: Final[int] = 4
EXIT_UNSUPPORTED: Final[int] = 5
EXIT_CONFIG_ERROR: Final[int] = 6
EXIT_PARTIAL_FAILURE: Final[int] = 7
EXIT_USAGE_ERROR: Final[int] = 64
EXIT_SIGINT: Final[int] = 130
EXIT_SIGTERM: Final[int] = 143


# ---------------------------------------------------------------------------
# Stable error-name enum (closed set per SRD §11.2)
# ---------------------------------------------------------------------------

ERROR_NAMES: Final[frozenset[str]] = frozenset(
    {
        "device_error",
        "auth_failed",
        "network_error",
        "not_found",
        "unsupported_feature",
        "config_error",
        "partial_failure",
        "usage_error",
        "interrupted",
    }
)


# ---------------------------------------------------------------------------
# Structured error payload (SRD §11.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StructuredError:
    """Stable JSON shape emitted to stderr on failure.

    Tooling MAY pattern-match on ``error``; the field set is a closed enum
    (see :data:`ERROR_NAMES`). All optional fields are omitted from the wire
    form when ``None``/empty.
    """

    error: str
    exit_code: int
    message: str
    target: str | None = None
    hint: str | None = None
    mechanism: str | None = None
    """Free-text label naming the underlying step that failed (e.g.
    ``"camera_account"``, ``"cloud_account"``, ``"tier-3-ffmpeg"``). Optional."""

    credential: str | None = None
    """For auth errors: which credential family failed
    (``"camera_account"`` | ``"cloud_account"``). Optional, §11.2."""

    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.error not in ERROR_NAMES:
            raise ValueError(
                f"unknown structured error name: {self.error!r}; "
                f"must be one of {sorted(ERROR_NAMES)}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable dict, omitting null/empty optional fields."""
        payload: dict[str, Any] = {
            "error": self.error,
            "exit_code": self.exit_code,
            "message": self.message,
        }
        if self.target is not None:
            payload["target"] = self.target
        if self.hint is not None:
            payload["hint"] = self.hint
        if self.mechanism is not None:
            payload["mechanism"] = self.mechanism
        if self.credential is not None:
            payload["credential"] = self.credential
        if self.extra:
            payload["details"] = dict(self.extra)
        return payload

    def to_json(self) -> str:
        """Return the canonical single-line JSON form for stderr emission."""
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=False)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> StructuredError:
        """Round-trip parse. Used by tests to assert JSON stability."""
        return cls(
            error=payload["error"],
            exit_code=int(payload["exit_code"]),
            message=payload["message"],
            target=payload.get("target"),
            hint=payload.get("hint"),
            mechanism=payload.get("mechanism"),
            credential=payload.get("credential"),
            extra=dict(payload.get("details", {})),
        )

    def asdict_full(self) -> dict[str, Any]:
        """Return all fields including null optionals (debug only)."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class TapoCliError(Exception):
    """Base class for all CLI failures that map to a non-zero exit code.

    Subclasses fix their own ``exit_code`` and ``error`` enum string.
    Carrying ``target``/``hint``/``mechanism``/``credential`` lets the
    dispatcher build a fully populated :class:`StructuredError` without
    further plumbing.
    """

    exit_code: int = EXIT_DEVICE_ERROR
    error_name: str = "device_error"

    def __init__(
        self,
        message: str,
        *,
        target: str | None = None,
        hint: str | None = None,
        mechanism: str | None = None,
        credential: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.target = target
        self.hint = hint
        self.mechanism = mechanism
        self.credential = credential
        self.extra: dict[str, Any] = dict(extra) if extra else {}

    def to_structured(self) -> StructuredError:
        """Project the exception onto the wire-format error object."""
        return StructuredError(
            error=self.error_name,
            exit_code=self.exit_code,
            message=self.message,
            target=self.target,
            hint=self.hint,
            mechanism=self.mechanism,
            credential=self.credential,
            extra=self.extra,
        )


class DeviceError(TapoCliError):
    """Camera returned an error response (non-auth, non-network). Exit 1."""

    exit_code = EXIT_DEVICE_ERROR
    error_name = "device_error"


class AuthError(TapoCliError):
    """Camera-account or cloud-account auth failed; chmod-mode violation;
    missing credentials when none configured. Exit 2.
    """

    exit_code = EXIT_AUTH_ERROR
    error_name = "auth_failed"


class NetworkError(TapoCliError):
    """Timeout, connection refused, no route, multicast bind failure,
    concurrent-lock acquisition timeout. Exit 3.
    """

    exit_code = EXIT_NETWORK_ERROR
    error_name = "network_error"


class NotFoundError(TapoCliError):
    """Alias unknown, IP unreachable, MAC not on LAN, unknown preset name.
    Exit 4.
    """

    exit_code = EXIT_NOT_FOUND
    error_name = "not_found"


class UnsupportedFeatureError(TapoCliError):
    """Verb/flag combo not supported by target model or firmware. Exit 5."""

    exit_code = EXIT_UNSUPPORTED
    error_name = "unsupported_feature"


class ConfigError(TapoCliError):
    """Bad TOML, missing required file, unresolvable refs, ffmpeg not on
    PATH (snapshot tier-3), invalid CIDR for ``--target-network``. Exit 6.
    """

    exit_code = EXIT_CONFIG_ERROR
    error_name = "config_error"


class PartialFailureError(TapoCliError):
    """Mixed-result batch/group: at least one ok, at least one fail. Exit 7."""

    exit_code = EXIT_PARTIAL_FAILURE
    error_name = "partial_failure"


class UsageError(TapoCliError):
    """Invalid CLI invocation: missing arg, mutually-exclusive flags,
    record/non-tty without --duration|--max-bytes, etc. Exit 64.
    """

    exit_code = EXIT_USAGE_ERROR
    error_name = "usage_error"


class TapoInterruptError(TapoCliError):
    """SIGINT/SIGTERM during execution. Exit 130 or 143 — caller picks.

    Named to avoid shadowing the Python builtin ``InterruptedError``.
    """

    exit_code = EXIT_SIGINT
    error_name = "interrupted"


__all__ = [
    "ERROR_NAMES",
    "EXIT_AUTH_ERROR",
    "EXIT_CONFIG_ERROR",
    "EXIT_DEVICE_ERROR",
    "EXIT_NETWORK_ERROR",
    "EXIT_NOT_FOUND",
    "EXIT_PARTIAL_FAILURE",
    "EXIT_SIGINT",
    "EXIT_SIGTERM",
    "EXIT_SUCCESS",
    "EXIT_UNSUPPORTED",
    "EXIT_USAGE_ERROR",
    "AuthError",
    "ConfigError",
    "DeviceError",
    "NetworkError",
    "NotFoundError",
    "PartialFailureError",
    "StructuredError",
    "TapoCliError",
    "TapoInterruptError",
    "UnsupportedFeatureError",
    "UsageError",
]
