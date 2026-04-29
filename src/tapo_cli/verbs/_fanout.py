"""Shared @group → per-alias fan-out helper for Phase 3 (FR-39..43, B9).

When a camera-control verb sees a target string that, after stripping the
leading ``@``, matches a configured group name, the verb expands the
group to its member aliases and runs the per-target coroutine across
them with bounded concurrency (default 5; CLI ``--concurrency N``
override).

Per-target results are emitted as JSONL (one line per member) in the
order of the **resolved alias list** (config-file ordering — NOT
completion order). The exit code follows FR-43a / B9:

* All ok → 0.
* Mixed → 7 (partial-failure).
* All fail → exit code of the FIRST member in the resolved list.

Verbs use this only for read-style or stateful-toggle ops that benefit
from parallelism. Footgun verbs (``stream`` / ``record`` per FR-43c)
explicitly reject group syntax instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from tapo_cli.config import Config
from tapo_cli.errors import (
    EXIT_PARTIAL_FAILURE,
    EXIT_SUCCESS,
    StructuredError,
    TapoCliError,
)
from tapo_cli.output import OutputMode

logger = logging.getLogger("tapo_cli")


# A per-target async function: takes the resolved alias and returns either
# (rc, record-dict) on success or raises TapoCliError on failure.
PerTargetFn = Callable[[str], Awaitable[tuple[int, dict[str, Any]]]]


def is_group_target(target: str, cfg: Config) -> bool:
    """True if ``target`` (with optional leading ``@``) names a config group."""
    resolved = target.lstrip("@") or target
    return resolved in cfg.groups


def group_members(target: str, cfg: Config) -> list[str]:
    """Return the resolved alias list for ``target``. Empty if not a group."""
    resolved = target.lstrip("@") or target
    return list(cfg.groups.get(resolved, []))


async def run_fanout(
    *,
    members: list[str],
    per_target: PerTargetFn,
    concurrency: int,
    mode: OutputMode,
) -> int:
    """Run ``per_target`` for each member with bounded concurrency.

    Emits one JSONL line per member on stdout in resolved-alias order
    (B9 deterministic). Returns the FR-43a / B9 exit code.

    The ``per_target`` callable is responsible for its own per-target
    business logic; this helper adds the result-record envelope
    ``{target, status, exit_code, result?, error?}`` so the operator's
    ``jq`` patterns over fan-out output are consistent with batch's B10
    shape.
    """
    if not members:
        return EXIT_SUCCESS

    sem = asyncio.Semaphore(max(1, concurrency))

    async def _runner(alias: str) -> dict[str, Any]:
        async with sem:
            try:
                rc, record = await per_target(alias)
                return {
                    "target": alias,
                    "status": "ok" if rc == 0 else "error",
                    "exit_code": rc,
                    "result": record,
                }
            except TapoCliError as exc:
                err = exc.to_structured()
                return {
                    "target": alias,
                    "status": "error",
                    "exit_code": exc.exit_code,
                    "error": _project_error(err),
                }
            except Exception as exc:
                logger.warning("fan-out exception for %s: %s", alias, exc)
                return {
                    "target": alias,
                    "status": "error",
                    "exit_code": 1,
                    "error": {
                        "code": "device_error",
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                }

    # asyncio.gather preserves input order in the result list; pair each
    # task with its index and re-sort to be defensive (B9: deterministic
    # by resolved-alias-list order, NOT completion order).
    tasks = [asyncio.create_task(_runner(alias)) for alias in members]
    results: list[dict[str, Any]] = []
    completed = await asyncio.gather(*tasks, return_exceptions=False)
    # ``completed`` is in input order because asyncio.gather preserves it.
    results = list(completed)

    # Emit JSONL on stdout (one line per member). Mode is honoured for
    # compatibility, but JSONL is the documented contract for fan-out.
    for record in results:
        if mode is OutputMode.QUIET:
            continue
        sys.stdout.write(json.dumps(record, separators=(",", ":")) + "\n")
    sys.stdout.flush()

    return _compute_exit_code(results)


def _project_error(err: StructuredError) -> dict[str, Any]:
    """Project the structured error onto the FR-44a / B10 sub-shape."""
    payload: dict[str, Any] = {
        "code": err.error,
        "message": err.message,
    }
    if err.hint is not None:
        payload["hint"] = err.hint
    return payload


def _compute_exit_code(results: list[dict[str, Any]]) -> int:
    """FR-43a / B9 exit-code derivation."""
    if not results:
        return EXIT_SUCCESS
    fail_count = sum(1 for r in results if r.get("status") == "error")
    ok_count = len(results) - fail_count
    if fail_count == 0:
        return EXIT_SUCCESS
    if ok_count == 0:
        rc = results[0].get("exit_code", 1)
        return int(rc) if isinstance(rc, int) else 1
    return EXIT_PARTIAL_FAILURE


__all__ = [
    "PerTargetFn",
    "group_members",
    "is_group_target",
    "run_fanout",
]
