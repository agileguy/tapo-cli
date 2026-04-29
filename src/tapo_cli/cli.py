"""Click-based CLI surface for tapo-cli (SRD §8).

The top-level group registers every verb (auth/config/discover/list/info
through Phase 1b; snapshot/stream/record/ptz/etc in later phases) and:

* configures stderr JSON-line logging (``-v`` → INFO, ``-vv`` → DEBUG,
  default WARNING).
* threads a state dict (mode, timeout, config_path, credential_source,
  …) into the Click context so verb modules don't re-parse top-level
  flags.

The async runner with TapoCliError → exit-code mapping lives in
:mod:`tapo_cli.runner` so verb modules can import it without circling
back through this file.
"""

from __future__ import annotations

import contextlib
import logging
import sys

import click

import tapo_cli as _pkg
from tapo_cli.errors import EXIT_USAGE_ERROR
from tapo_cli.output import detect_mode
from tapo_cli.runner import run_async as _run_async
from tapo_cli.verbs.alarm_cmd import alarm_cmd
from tapo_cli.verbs.audio_cmd import audio_cmd
from tapo_cli.verbs.auth_cmd import auth_group
from tapo_cli.verbs.config_cmd import config_group
from tapo_cli.verbs.discover_cmd import discover_cmd
from tapo_cli.verbs.info_cmd import info_cmd
from tapo_cli.verbs.led_cmd import led_cmd
from tapo_cli.verbs.list_cmd import list_cmd
from tapo_cli.verbs.motion_cmd import motion_cmd
from tapo_cli.verbs.night_vision_cmd import night_vision_cmd
from tapo_cli.verbs.osd_cmd import osd_cmd
from tapo_cli.verbs.preset_cmd import preset_cmd
from tapo_cli.verbs.privacy_cmd import privacy_cmd
from tapo_cli.verbs.ptz_cmd import ptz_cmd
from tapo_cli.verbs.reboot_cmd import reboot_cmd
from tapo_cli.verbs.snapshot_cmd import snapshot_cmd
from tapo_cli.verbs.stream_cmd import stream_cmd

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
        # The stream verb (FR-12) needs to distinguish "explicit --json/--jsonl"
        # from "auto-JSONL on a pipe" because its default contract is a bare
        # ``rtsp://...`` line on stdout regardless of tty state. Preserve the
        # raw flags so a verb that wants this distinction can ask for it
        # without re-parsing argv.
        "json_flag": json_flag,
        "jsonl_flag": jsonl_flag,
        "quiet_flag": quiet,
    }


main.add_command(auth_group)
main.add_command(config_group)
main.add_command(discover_cmd)
main.add_command(list_cmd)
main.add_command(info_cmd)
main.add_command(snapshot_cmd)
main.add_command(stream_cmd)
main.add_command(privacy_cmd)
main.add_command(led_cmd)
main.add_command(night_vision_cmd)
main.add_command(motion_cmd)
main.add_command(reboot_cmd)
# Phase 2 verbs (FR-14..17, FR-18..21, FR-22..24, FR-33..36, FR-37).
main.add_command(ptz_cmd)
main.add_command(preset_cmd)
main.add_command(alarm_cmd)
main.add_command(audio_cmd)
main.add_command(osd_cmd)


# Re-export the runner under its old name for any downstream call site that
# imports ``_run_async`` from this module (kept for ABI continuity).
__all__ = ["_run_async", "main"]
