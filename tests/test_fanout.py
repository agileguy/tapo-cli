"""Tests for the @group fan-out helper (Phase 3, FR-39..43, B9)."""

from __future__ import annotations

import asyncio
import io
import json
from contextlib import redirect_stdout

from tapo_cli.config import Config, DeviceEntry
from tapo_cli.errors import (
    EXIT_PARTIAL_FAILURE,
    EXIT_SUCCESS,
    EXIT_USAGE_ERROR,
    UsageError,
)
from tapo_cli.output import OutputMode
from tapo_cli.verbs._fanout import (
    group_members,
    is_group_target,
    run_fanout,
)


def _cfg(group: list[str]) -> Config:
    cfg = Config()
    for alias in group:
        cfg.devices[alias] = DeviceEntry(alias=alias, ip=f"10.0.0.{len(cfg.devices) + 1}")
    cfg.groups["all"] = list(group)
    return cfg


def test_is_group_target_strips_at_prefix() -> None:
    cfg = _cfg(["a", "b"])
    assert is_group_target("@all", cfg) is True
    assert is_group_target("all", cfg) is True
    assert is_group_target("nope", cfg) is False


def test_group_members_returns_resolved_alias_list() -> None:
    cfg = _cfg(["a", "b", "c"])
    assert group_members("@all", cfg) == ["a", "b", "c"]


def _drive(coro):
    return asyncio.run(coro)


def test_fanout_all_ok_returns_zero_and_emits_three_lines() -> None:
    async def _per_target(alias: str) -> tuple[int, dict[str, object]]:
        return 0, {"alias": alias}

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = _drive(
            run_fanout(
                members=["a", "b", "c"],
                per_target=_per_target,
                concurrency=2,
                mode=OutputMode.JSONL,
            )
        )
    assert rc == EXIT_SUCCESS
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    # B9: ordering matches resolved-alias list (input order).
    assert [r["target"] for r in parsed] == ["a", "b", "c"]
    for r in parsed:
        assert r["status"] == "ok"


def test_fanout_partial_failure_returns_7() -> None:
    async def _per_target(alias: str) -> tuple[int, dict[str, object]]:
        if alias == "b":
            raise UsageError(f"bad {alias}", target=alias, hint="fix it")
        return 0, {"alias": alias}

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = _drive(
            run_fanout(
                members=["a", "b", "c"],
                per_target=_per_target,
                concurrency=2,
                mode=OutputMode.JSONL,
            )
        )
    assert rc == EXIT_PARTIAL_FAILURE
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    statuses = [json.loads(line)["status"] for line in lines]
    assert statuses.count("ok") == 2
    assert statuses.count("error") == 1


def test_fanout_all_fail_returns_first_error_code() -> None:
    """B9: all-fail returns the exit code of the FIRST member's failure,
    NOT the first to complete."""

    async def _per_target(alias: str) -> tuple[int, dict[str, object]]:
        # Different exit codes per alias to prove B9 picks the FIRST.
        await asyncio.sleep(0)
        if alias == "a":
            raise UsageError("a-fail", target=alias)  # exit 64
        if alias == "b":
            raise UsageError("b-fail", target=alias)
        raise UsageError("c-fail", target=alias)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = _drive(
            run_fanout(
                members=["a", "b", "c"],
                per_target=_per_target,
                concurrency=3,
                mode=OutputMode.JSONL,
            )
        )
    # All-fail → first member's exit code (UsageError → 64).
    assert rc == EXIT_USAGE_ERROR


def test_fanout_quiet_mode_emits_no_stdout() -> None:
    async def _per_target(alias: str) -> tuple[int, dict[str, object]]:
        return 0, {"alias": alias}

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = _drive(
            run_fanout(
                members=["a", "b"],
                per_target=_per_target,
                concurrency=1,
                mode=OutputMode.QUIET,
            )
        )
    assert rc == EXIT_SUCCESS
    assert buf.getvalue() == ""


def test_fanout_empty_members_returns_zero() -> None:
    async def _per_target(alias: str) -> tuple[int, dict[str, object]]:
        raise AssertionError("should not be called")

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = _drive(
            run_fanout(
                members=[],
                per_target=_per_target,
                concurrency=2,
                mode=OutputMode.JSONL,
            )
        )
    assert rc == EXIT_SUCCESS
    assert buf.getvalue() == ""
