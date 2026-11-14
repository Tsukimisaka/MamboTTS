"""Unified package entrypoint (``python -m mambotts``).

Routes empty argv / ``--gui`` to the PySide6 studio (lazily imported so the
headless CLI never touches PySide6); everything else goes to the CLI.
"""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] == "--gui":
        from mambotts.ui.app_shell import run_gui

        return run_gui(args)
    from mambotts.cli import run_cli

    return run_cli(args)


if __name__ == "__main__":
    sys.exit(main())
