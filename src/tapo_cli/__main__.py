"""Entry point for the ``tapo-cli`` console script.

The async event loop is started inside individual verb runners via
``asyncio.run``. This module is a thin shim that converts Click's exceptions
(raised when ``standalone_mode=False``) into a process exit code with the
SRD-shaped structured-error envelope on stderr.
"""

from __future__ import annotations

import click

from tapo_cli.cli import main as _cli_main
from tapo_cli.errors import EXIT_USAGE_ERROR, StructuredError
from tapo_cli.output import OutputMode, emit_error


def main() -> int:
    """Run the CLI and return the desired process exit code."""
    try:
        result = _cli_main(standalone_mode=False)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 0
        return int(code)
    except click.exceptions.Exit as exc:
        return int(exc.exit_code)
    except click.UsageError as exc:
        # Click raises UsageError when standalone_mode=False; translate to a
        # SRD-shaped structured error on stderr.
        err = StructuredError(
            error="usage_error",
            exit_code=EXIT_USAGE_ERROR,
            target=None,
            message=str(exc),
            hint="Run with --help for usage.",
        )
        emit_error(err, OutputMode.JSONL)
        return EXIT_USAGE_ERROR
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    if isinstance(result, int):
        return result
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
