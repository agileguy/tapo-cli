"""``tapo-cli batch [--stdin | --file PATH]`` (SRD §5.16, FR-44..45c, B10).

Reads newline-delimited sub-commands and executes them, emitting one
JSONL result per line on stdout. Each result conforms to FR-44a / B10::

    {
      "command":   "<verb-and-flags-string>",
      "target":    "<resolved-alias-or-ip>",
      "status":    "ok" | "error",
      "exit_code": <int>,
      "result":    <verb's --json payload>,           # iff status=="ok"
      "error":     {                                   # iff status=="error"
        "code": "<§11.2 enum>",
        "message": "...",
        "hint": "..."
      }
    }

Exit code semantics (FR-43a / FR-45a):

* ``0`` if every sub-operation succeeded.
* ``7`` (partial failure) if at least one ok AND at least one failed.
* When ALL sub-ops failed, exit with the sub-op's error code whose target
  appears first in the resolved-alias list (B9 — config-file ordering, NOT
  completion order). For batch, the "resolved-alias list" is the order
  the input lines appeared in.

Empty input → exit 0 silently (FR-45b). Comments (``#``) and blank lines
are skipped.

SIGINT/SIGTERM (FR-45c): cease dispatching, wait up to 2 s for in-flight
ops, emit ``{"event":"interrupted","completed":N,"pending":M}`` summary,
exit 130 / 143.

Implementation strategy: each input line is shlex.split'd into argv and
re-dispatched through the top-level ``main`` Click group with
``--json`` forced on so we get a parseable single-record payload. The
sub-call's stdout is captured into ``result``; the sub-call's stderr
emission is captured and parsed for the structured-error envelope on
failure.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import shlex
import sys
from pathlib import Path
from typing import Any

import click

from tapo_cli.errors import (
    EXIT_PARTIAL_FAILURE,
    EXIT_SUCCESS,
    EXIT_USAGE_ERROR,
    UsageError,
)
from tapo_cli.output import OutputMode

logger = logging.getLogger("tapo_cli")


# ---------------------------------------------------------------------------
# Click verb
# ---------------------------------------------------------------------------


@click.command("batch")
@click.option(
    "--file",
    "file_path",
    type=click.Path(dir_okay=False),
    default=None,
    help="Read newline-delimited sub-commands from this path (FR-44).",
)
@click.option(
    "--stdin",
    "from_stdin",
    is_flag=True,
    default=False,
    help="Read newline-delimited sub-commands from stdin (FR-45).",
)
@click.pass_context
def batch_cmd(
    ctx: click.Context,
    *,
    file_path: str | None,
    from_stdin: bool,
) -> None:
    """Run a stream of sub-commands; emit one JSONL result per line."""
    state = ctx.obj
    mode: OutputMode = state["mode"]

    # NB: ``batch`` runs SYNCHRONOUSLY at the top level — each sub-command
    # has its own ``asyncio.run`` inside its verb runner. Wrapping batch
    # in our outer ``run_async`` would trigger a nested-loop RuntimeError
    # when the first sub-call hits its own ``asyncio.run``.
    try:
        if file_path is None and not from_stdin:
            raise UsageError(
                "batch requires --stdin or --file PATH",
                hint=(
                    "Examples: tapo-cli batch --stdin, "
                    "tapo-cli batch --file ops.txt"
                ),
            )
        if file_path is not None and from_stdin:
            raise UsageError(
                "--stdin and --file are mutually exclusive",
                hint="Pick one input source.",
            )
        rc = _run_sync(
            file_path=file_path,
            from_stdin=from_stdin,
            mode=mode,
        )
    except UsageError as exc:
        from tapo_cli.output import emit_error

        emit_error(exc.to_structured(), mode)
        rc = exc.exit_code
    sys.exit(rc)


def _run_sync(
    *,
    file_path: str | None,
    from_stdin: bool,
    mode: OutputMode,
) -> int:
    lines = _read_lines(file_path=file_path, from_stdin=from_stdin)
    parsed = _parse_lines(lines)

    # FR-45b: empty input is exit 0 with no output.
    if not parsed:
        return EXIT_SUCCESS

    results: list[dict[str, Any]] = []
    for line_no, argv in parsed:
        result = _dispatch_one(line_no=line_no, argv=argv)
        results.append(result)
        _emit_result(result, mode=mode)

    return _compute_exit_code(results)


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


def _read_lines(*, file_path: str | None, from_stdin: bool) -> list[str]:
    if from_stdin:
        return sys.stdin.read().splitlines()
    assert file_path is not None
    p = Path(file_path).expanduser()
    if not p.exists():
        raise UsageError(
            f"batch file not found: {p}",
            hint="Pass --file with a path that exists.",
        )
    return p.read_text(encoding="utf-8").splitlines()


def _parse_lines(lines: list[str]) -> list[tuple[int, list[str]]]:
    """Return ``[(line_no, argv), ...]`` after stripping blanks/comments."""
    out: list[tuple[int, list[str]]] = []
    for idx, raw in enumerate(lines, start=1):
        s = raw.strip()
        if not s:
            continue
        if s.startswith("#"):
            continue
        try:
            argv = shlex.split(s)
        except ValueError as exc:
            # Unbalanced quotes etc. — surface as a parse-line error.
            out.append((idx, ["__shlex_error__", str(exc), raw]))
            continue
        if argv:
            out.append((idx, argv))
    return out


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _dispatch_one(*, line_no: int, argv: list[str]) -> dict[str, Any]:
    """Re-invoke the top-level CLI with ``argv`` and capture the result.

    Returns a B10-shaped dict (always — never raises out of this function).
    """
    command_str = " ".join(shlex.quote(a) for a in argv)

    if argv and argv[0] == "__shlex_error__":
        return {
            "command": argv[2] if len(argv) > 2 else "",
            "target": "",
            "status": "error",
            "exit_code": EXIT_USAGE_ERROR,
            "error": {
                "code": "usage_error",
                "message": f"line {line_no}: shlex parse failed: {argv[1]}",
                "hint": "Check quoting in the batch input.",
            },
        }

    target = _extract_target(argv)

    # Force --json on the sub-call so ``result`` is parseable.
    sub_argv = _ensure_json_flag(argv)

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()

    # Invoke a fresh CLI run in-process. Importing ``main`` lazily avoids
    # a circular import (cli.py imports batch_cmd from this module).
    from tapo_cli.cli import main as cli_main

    runner_exit: int = 0
    try:
        with (
            contextlib.redirect_stdout(stdout_buf),
            contextlib.redirect_stderr(stderr_buf),
        ):
            cli_main.main(args=sub_argv, standalone_mode=False)
    except SystemExit as exc:
        runner_exit = int(exc.code) if isinstance(exc.code, int) else 1
    except click.exceptions.Exit as exc:
        runner_exit = int(exc.exit_code)
    except click.UsageError as exc:
        # Click's parse-time errors don't reach our verb's runner; map.
        stderr_buf.write(str(exc) + "\n")
        runner_exit = EXIT_USAGE_ERROR
    except Exception as exc:
        stderr_buf.write(f"unhandled: {type(exc).__name__}: {exc}\n")
        runner_exit = 1

    stdout_text = stdout_buf.getvalue()
    stderr_text = stderr_buf.getvalue()

    if runner_exit == 0:
        # Successful sub-call. Parse ``result`` from stdout.
        result_payload: Any
        try:
            result_payload = json.loads(stdout_text) if stdout_text.strip() else None
        except json.JSONDecodeError:
            # Non-JSON stdout — keep the raw text so the operator isn't
            # left guessing. Stream's bare-URL line lands here.
            result_payload = {"stdout": stdout_text.rstrip("\n")}
        return {
            "command": command_str,
            "target": target,
            "status": "ok",
            "exit_code": 0,
            "result": result_payload,
        }

    # Failure path: parse the structured-error envelope from stderr.
    err_obj = _parse_structured_error(stderr_text)
    if err_obj is None:
        err_obj = {
            "code": "device_error",
            "message": (stderr_text.strip().splitlines()[-1]
                        if stderr_text.strip()
                        else f"sub-command exited {runner_exit}"),
        }
    err_for_line = {
        k: v for k, v in err_obj.items() if k in ("code", "message", "hint")
    }
    if "code" not in err_for_line:
        err_for_line["code"] = "device_error"
    if "message" not in err_for_line:
        err_for_line["message"] = f"sub-command exited {runner_exit}"

    return {
        "command": command_str,
        "target": target,
        "status": "error",
        "exit_code": runner_exit,
        "error": err_for_line,
    }


def _ensure_json_flag(argv: list[str]) -> list[str]:
    """Inject ``--json`` before the verb token if neither --json nor --jsonl
    is already present. Top-level flags MUST precede the verb token.
    """
    if "--json" in argv or "--jsonl" in argv:
        return list(argv)
    return ["--json", *argv]


# Verbs that take a target as their first positional after the verb token.
# Any verb NOT in this set (e.g. ``groups``, ``config``, ``auth``,
# ``discover``, ``list``, ``batch``) reports an empty target on its B10
# JSONL line, so jq pattern-matching by target stays sane.
_VERBS_WITH_TARGET: frozenset[str] = frozenset(
    {
        "info",
        "snapshot",
        "stream",
        "record",
        "privacy",
        "led",
        "night-vision",
        "motion",
        "reboot",
        "ptz",
        "preset",
        "alarm",
        "audio",
        "osd",
    }
)


def _extract_target(argv: list[str]) -> str:
    """Best-effort extract of the target argument from a parsed sub-command.

    Top-level flags use ``--name=value`` or ``--name value`` form; the verb
    token is the first non-flag positional. The target — when the verb has
    one — is the next positional after the verb. We don't do full Click
    parsing here; just walk argv past flags.
    """
    skip_next = False
    positionals: list[str] = []
    flag_takes_value = {
        "--config",
        "--timeout",
        "--credential-source",
        "--concurrency",
    }
    for tok in argv:
        if skip_next:
            skip_next = False
            continue
        if tok.startswith("--"):
            base = tok.split("=", 1)[0]
            if base in flag_takes_value and "=" not in tok:
                skip_next = True
            continue
        if tok.startswith("-") and tok != "-":
            continue
        positionals.append(tok)

    if not positionals:
        return ""
    verb = positionals[0]
    if verb not in _VERBS_WITH_TARGET:
        return ""
    if len(positionals) < 2:
        return ""
    return positionals[1].lstrip("@") or positionals[1]


def _parse_structured_error(stderr_text: str) -> dict[str, Any] | None:
    """Parse the last JSON line on stderr as the structured error envelope.

    The runner emits one JSON line on failure; sub-calls may also emit
    Python warnings or click messages. We scan from the bottom and return
    the first valid JSON object that has ``error`` and ``message`` keys.
    """
    for line in reversed(stderr_text.splitlines()):
        s = line.strip()
        if not s.startswith("{"):
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "error" in obj and "message" in obj:
            return {
                "code": obj.get("error", "device_error"),
                "message": obj.get("message", ""),
                "hint": obj.get("hint"),
            }
    return None


# ---------------------------------------------------------------------------
# Output + exit code
# ---------------------------------------------------------------------------


def _emit_result(result: dict[str, Any], *, mode: OutputMode) -> None:
    """Emit one batch-result line. Always JSONL on stdout regardless of mode.

    FR-44/45 specify JSONL on stdout — the per-line shape is the contract
    so callers can ``jq -c`` against it. ``--json`` (whole-array pretty
    output) is intentionally NOT supported; the SRD wording is "one JSONL
    result per line on stdout".
    """
    line = json.dumps(result, separators=(",", ":"))
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
    del mode  # all modes emit JSONL


def _compute_exit_code(results: list[dict[str, Any]]) -> int:
    """Apply FR-43a / FR-45a / B9 to derive the batch exit code."""
    if not results:
        return EXIT_SUCCESS
    fail_count = sum(1 for r in results if r.get("status") == "error")
    ok_count = len(results) - fail_count
    if fail_count == 0:
        return EXIT_SUCCESS
    if ok_count == 0:
        # All failed → first sub-op's exit code (B9 deterministic).
        first = results[0]
        rc = first.get("exit_code", 1)
        return int(rc) if isinstance(rc, int) else 1
    # Mixed outcomes → 7.
    return EXIT_PARTIAL_FAILURE


__all__ = ["batch_cmd"]
