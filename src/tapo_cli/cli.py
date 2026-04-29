"""Click-based CLI surface for tapo-cli (SRD §8).

Phase 1a wires up only the meta verbs (``auth``, ``config``); camera verbs
ship in Phase 1b/1c/1d. The top-level group:

* maps every :class:`TapoCliError` subclass to its fixed exit code (§11.1),
* installs SIGINT/SIGTERM handlers (Phase 1a: convert to exit 130/143; the
  full graceful-drain behavior of FR-31c is later phases),
* configures stderr JSON-line logging (`-v` → INFO, `-vv` → DEBUG, default
  WARNING).

The async runner sits here and not in __main__ because individual verb
runners may want to share it (Phase 1b's ``discover`` will).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Callable, Coroutine
from typing import Any

import click

import tapo_cli as _pkg
from tapo_cli.errors import (
    EXIT_SIGINT,
    EXIT_SIGTERM,
    EXIT_USAGE_ERROR,
    StructuredError,
    TapoCliError,
)
from tapo_cli.output import OutputMode, detect_mode, emit_error
from tapo_cli.verbs.auth_cmd import auth_group
from tapo_cli.verbs.config_cmd import config_group

# ---------------------------------------------------------------------------
# Async runner with TapoCliError → exit-code mapping
# ---------------------------------------------------------------------------


def _run_async(
    coro_factory: Callable[[], Coroutine[Any, Any, int]],
    *,
    mode: OutputMode,
) -> int:
    """Run an async coroutine factory, mapping errors to exit codes.

    Signal handling: install handlers that re-raise as ``KeyboardInterrupt``
    for SIGINT or ``SystemExit(143)`` for SIGTERM. Phase 1a does not yet do
    graceful batch/group drain — that is later phases.
    """

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


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


_LOG_FORMAT: str = (
    '{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}'
)


def _log_formatter() -> logging.Formatter:
    return logging.Formatter(_LOG_FORMAT)


def _configure_logging(verbose: int) -> None:
    """Wire ``-v`` / ``-vv`` to a stderr JSON-line StreamHandler (§7.3).

    Default WARNING (silent on success); ``-v`` lifts to INFO; ``-vv`` to
    DEBUG. Re-entrant safe — clears prior handlers if ``main`` is invoked
    twice in the same process (tests do this).
    """
    if verbose >= 2:
        level = logging.DEBUG
    elif verbose == 1:
        level = logging.INFO
    else:
        level = logging.WARNING

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_log_formatter())
    root = logging.getLogger("tapo_cli")
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
        if isinstance(h, logging.FileHandler):
            with contextlib.suppress(Exception):
                h.close()
    root.addHandler(handler)
    root.propagate = True


# ---------------------------------------------------------------------------
# Top-level Click group
# ---------------------------------------------------------------------------


@click.group(name="tapo-cli", invoke_without_command=False)
@click.option("--json", "json_flag", is_flag=True, default=False, help="Pretty JSON output.")
@click.option("--jsonl", "jsonl_flag", is_flag=True, default=False, help="JSON-lines output.")
@click.option("--quiet", is_flag=True, default=False, help="Suppress stdout (text verbs only).")
@click.option(
    "--timeout",
    "timeout",
    type=float,
    default=5.0,
    show_default=True,
    help="Per-operation timeout in seconds.",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False),
    default=None,
    help="Path to a non-default config file (also via TAPO_CLI_CONFIG).",
)
@click.option(
    "--concurrency",
    "global_concurrency",
    type=int,
    default=None,
    help="Override [defaults] concurrency for this invocation.",
)
@click.option(
    "--credential-source",
    type=click.Choice(["env", "file", "none"]),
    default=None,
    help=(
        "Constrain credential sources (FR-CRED-15). "
        "env: only TAPO_USERNAME/PASSWORD. "
        "file: only file-based sources. "
        "none: skip all sources."
    ),
)
@click.option("-v", "verbose", count=True, help="-v / -vv stderr verbosity.")
@click.version_option(version=_pkg.__version__, prog_name="tapo-cli")
@click.pass_context
def main(
    ctx: click.Context,
    *,
    json_flag: bool,
    jsonl_flag: bool,
    quiet: bool,
    timeout: float,
    config_path: str | None,
    global_concurrency: int | None,
    credential_source: str | None,
    verbose: int,
) -> None:
    """``tapo-cli`` — deterministic local-LAN CLI for TP-Link Tapo cameras."""
    if json_flag and jsonl_flag:
        click.echo("error: --json and --jsonl are mutually exclusive", err=True)
        ctx.exit(EXIT_USAGE_ERROR)

    _configure_logging(verbose)
    mode = detect_mode(json_flag=json_flag, jsonl_flag=jsonl_flag, quiet=quiet)
    ctx.obj = {
        "mode": mode,
        "timeout": timeout,
        "config_path": config_path,
        "credential_source": credential_source,
        "verbose": verbose,
        "concurrency": global_concurrency,
    }


# Register sub-verb groups. Camera verbs are intentionally NOT registered here
# yet — Phase 1b/1c/1d add them.
main.add_command(auth_group)
main.add_command(config_group)


# Re-exports. ``_run_async`` is exposed for Phase 1b verb modules.
__all__ = ["_run_async", "main"]
