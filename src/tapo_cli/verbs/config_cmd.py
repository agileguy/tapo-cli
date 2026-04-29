"""``tapo-cli config`` sub-verbs (FR-54a, 54c; SRD §6.9).

Two actions:

* ``config show`` — emit the resolved active config in canonical TOML.
  Passwords / secrets are NEVER in the Config object itself (they live in
  separate JSON files referenced by path), so the redaction surface is
  small in v1. The ``redact`` flag is reserved for future use should a
  field be added that does carry a secret.
* ``config validate [<path>]`` — standalone lint mode. Defaults to the
  active config when no path is passed; exits 0 on success or 6 with a
  structured envelope on failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from tapo_cli.config import effective_toml, load_config, validate_config
from tapo_cli.errors import (
    EXIT_SUCCESS,
    EXIT_USAGE_ERROR,
    StructuredError,
    TapoCliError,
)
from tapo_cli.output import emit_error


@click.group("config")
def config_group() -> None:
    """Configuration sub-verbs."""


@config_group.command("show")
@click.pass_context
def config_show_cmd(ctx: click.Context) -> None:
    """Print the resolved effective config as TOML.

    Passwords are redacted to ``***`` per S16. Currently there are none in
    the Config dataclass — credential files are referenced by path only —
    but the formatter is set up to keep that invariant.
    """
    state = ctx.obj
    cfg_path = _config_path(state)
    try:
        cfg = load_config(cfg_path)
    except TapoCliError as exc:
        emit_error(exc.to_structured(), state["mode"])
        sys.exit(exc.exit_code)
    click.echo(effective_toml(cfg, redact=True), nl=False)
    sys.exit(EXIT_SUCCESS)


@config_group.command("validate")
@click.argument("path", required=False, type=click.Path(dir_okay=False))
@click.pass_context
def config_validate_cmd(ctx: click.Context, *, path: str | None) -> None:
    """Validate a config file. Exit 0 on success, 6 on failure."""
    state = ctx.obj
    candidate = path or state.get("config_path")
    if candidate is None:
        # Default to the user's active config file if it exists; else error
        # cleanly with a usage message.
        from tapo_cli.config import _default_config_path  # local: tests can override $HOME

        default = _default_config_path()
        if not default.exists():
            err = StructuredError(
                error="usage_error",
                exit_code=EXIT_USAGE_ERROR,
                message=(
                    "config validate requires a path; no active config found at "
                    f"{default}"
                ),
                hint="Pass a path: tapo-cli config validate <path>",
            )
            emit_error(err, state["mode"])
            sys.exit(EXIT_USAGE_ERROR)
        candidate = str(default)

    try:
        validate_config(Path(candidate).expanduser())
        sys.exit(EXIT_SUCCESS)
    except TapoCliError as exc:
        emit_error(exc.to_structured(), state["mode"])
        sys.exit(exc.exit_code)


def _config_path(state: dict[str, object]) -> Path | None:
    raw = state.get("config_path")
    if raw is None:
        return None
    return Path(str(raw)).expanduser()


__all__ = ["config_group"]
