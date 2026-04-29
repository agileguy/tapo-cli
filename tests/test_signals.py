"""Tests for SIGINT/SIGTERM handling in the CLI runner (SRD §11, FR-13b).

These exercise :func:`tapo_cli.runner.run_async` directly because the
high-level CLI tests can't fire signals at the in-process Click runner
without spawning a subprocess. The runner contract:

* SIGINT during an async verb → exit 130 (FR-31c).
* SIGTERM → exit 143.
* Record's ffmpeg-grace path is exercised by ``test_record_cmd.py``;
  this module asserts the runner-level translation.
"""

from __future__ import annotations

import asyncio

from tapo_cli.errors import EXIT_SIGINT, EXIT_SIGTERM
from tapo_cli.output import OutputMode
from tapo_cli.runner import run_async


def test_keyboard_interrupt_in_async_verb_returns_130() -> None:
    async def _coro() -> int:
        raise KeyboardInterrupt

    rc = run_async(_coro, mode=OutputMode.JSONL)
    assert rc == EXIT_SIGINT


def test_systemexit_with_143_returns_143() -> None:
    async def _coro() -> int:
        raise SystemExit(EXIT_SIGTERM)

    rc = run_async(_coro, mode=OutputMode.JSONL)
    assert rc == EXIT_SIGTERM


def test_systemexit_with_string_code_falls_back_to_sigterm() -> None:
    async def _coro() -> int:
        raise SystemExit("aborted")

    rc = run_async(_coro, mode=OutputMode.JSONL)
    assert rc == EXIT_SIGTERM


def test_normal_completion_returns_zero() -> None:
    async def _coro() -> int:
        return 0

    rc = run_async(_coro, mode=OutputMode.JSONL)
    assert rc == 0


def test_unhandled_exception_returns_one() -> None:
    async def _coro() -> int:
        raise RuntimeError("kaboom")

    rc = run_async(_coro, mode=OutputMode.JSONL)
    assert rc == 1


def test_keyboard_interrupt_after_partial_work_still_exits_130() -> None:
    """Simulate the user hitting Ctrl-C mid-coroutine."""

    async def _coro() -> int:
        await asyncio.sleep(0)  # yield once
        raise KeyboardInterrupt

    rc = run_async(_coro, mode=OutputMode.JSONL)
    assert rc == EXIT_SIGINT
