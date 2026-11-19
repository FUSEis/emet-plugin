#!/usr/bin/env python3
"""Fails when a pull request changes the plugin's code and leaves CHANGELOG.md alone.

Reads the changed paths from stdin, separated by NUL as `git diff --name-only -z` writes them,
and the pull request's labels from PR_LABELS as a JSON list. See CONTRIBUTING.md.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Iterable, List

CODE = "claude/scripts/"
CHANGELOG = "CHANGELOG.md"
# The label of a pull request whose change to the code needs no entry
LABEL = "no-changelog"


def code_changed(paths: Iterable[str]) -> List[str]:
    """Returns the changed paths that need an entry, or none when the changelog changed too."""
    changed = [path for path in paths if path]
    if CHANGELOG in changed:
        return []
    return [path for path in changed if path.startswith(CODE)]


def labels(raw: str) -> List[str]:
    """Returns the label names in a JSON list. Anything else is no label."""
    try:
        found = json.loads(raw or "[]")
    except ValueError:
        return []
    return [name for name in found if isinstance(name, str)] if isinstance(found, list) else []


def main() -> int:
    missing = code_changed(sys.stdin.buffer.read().decode("utf-8", "replace").split("\0"))
    if not missing:
        return 0
    if LABEL in labels(os.environ.get("PR_LABELS", "")):
        print(f"No {CHANGELOG} entry, and the pull request carries the {LABEL} label.")
        return 0
    print(f"This pull request changes the plugin's code and not {CHANGELOG}:")
    for path in missing:
        print(f"  {path}")
    print(f"Add an entry under Unreleased, or add the {LABEL} label when the change alters nothing a user sees or Emet receives.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
