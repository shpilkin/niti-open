"""PyInstaller entry point for the bundled backend.

The macOS shell launches this binary with no arguments, which defaults to the
``app`` command: apply migrations, then run sync + worker + scheduler + bot + api
in one process. Any explicit subcommand (e.g. ``migrate``) is still honored.
"""

from __future__ import annotations

import sys

from email_reader.cli import main

if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.append("app")
    main()
