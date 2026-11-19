#!/usr/bin/env python3
"""Claude Code hook entry point for the Emet plugin. See README.md for what it sends."""

import sys
from pathlib import Path

if sys.version_info < (3, 9):  # noqa: UP036 - checked at run time, before anything newer is imported
    # Too old to run; stay silent rather than break the session
    sys.exit(0)

sys.path.insert(0, str(Path(__file__).resolve().parent))


def run() -> int:
    """Runs the hook. Whatever fails, the session sees exit 0 and no output."""
    try:
        from emet_claude.commands import main

        return main()
    except Exception:  # noqa: BLE001 - a hook must never break the session
        return 0


if __name__ == "__main__":
    sys.exit(run())
