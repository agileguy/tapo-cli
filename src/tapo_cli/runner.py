"""Async runner with TapoCliError → exit-code mapping (SRD §11.1).

Lifted out of :mod:`tapo_cli.cli` so verb modules can import the runner
without introducing a circular import on the Click group. Behaviour is
identical to the Phase 1a in-place implementation:

* SIGINT is converted to exit 130, SIGTERM to exit 143 (FR-31c later
  phases will add graceful drain — Phase 1a / 1b convert only).
* Any :class:`TapoCliError` subclass is mapped to its fixed exit code.
* Anything else is wrapped in a generic ``device_error`` envelope and
  exits 1.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Callable, Coroutine
from typing import Any

from tapo_cli.errors import (
    EXIT_SIGINT,
    EXIT_SIGTERM,
    StructuredError,
    TapoCliError,
)
from tapo_cli.output import OutputMode, emit_error


def run_async(
    coro_factory: Callable[[], Coroutine[Any, Any, int]],
    *,
    mode: OutputMode,
) -> int:
    """Run an async coroutine factory, mapping errors to exit codes."""

    def _handle_sigint(*_args: object) -> None:
        raise KeyboardInterrupt

    def _handle_sigterm(*_args: object) -> None:
        raise SystemExit(EXIT_SIGTERM)

    prior_int = signal.getsignal(signal.SIGINT)
    prior_term = signal.getsignal(signal.SIGTERM)
    try:
        signal.signal(signal.SIGINT, _handle_sigint)
        with contextlib.suppress(OSError, ValueError):
            signal.signal(signal.SIGTERM, _handle_sigterm)

        try:
            return asyncio.run(coro_factory())
        except KeyboardInterrupt:
            return EXIT_SIGINT
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else EXIT_SIGTERM
            return int(code)
        except TapoCliError as exc:
            emit_error(exc.to_structured(), mode)
            return exc.exit_code
        except Exception as exc:
            err = StructuredError(
                error="device_error",
                exit_code=1,
                message=f"Unhandled error: {type(exc).__name__}: {exc}",
            )
            emit_error(err, mode)
            return 1
    finally:
        signal.signal(signal.SIGINT, prior_int)
        with contextlib.suppress(OSError, ValueError):
            signal.signal(signal.SIGTERM, prior_term)


__all__ = ["run_async"]
