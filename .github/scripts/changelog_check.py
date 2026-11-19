#!/usr/bin/env python3
"""Fails when a pull request changes what the plugin ships and adds no entry to CHANGELOG.md.

Reads the changed paths from stdin, separated by NUL as `git diff --name-only -z` writes them,
and the pull request's labels from PR_LABELS as a JSON list. The first argument is a file holding
CHANGELOG.md as the base branch has it; the changelog of the pull request is read from the
current folder. See CONTRIBUTING.md.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Iterable, List, Set

# What Claude Code installs and runs: the hook script, the hooks, the skills and the manifest. Not the plugin's README
SHIPPED = ("claude/scripts/", "claude/hooks/", "claude/skills/", "claude/.claude-plugin/")
CHANGELOG = "CHANGELOG.md"
UNRELEASED = "## [Unreleased]"
VERSION = "## ["
# The label of a pull request whose change needs no entry
LABEL = "no-changelog"


def shipped(paths: Iterable[str]) -> List[str]:
    """Returns the changed paths that need an entry."""
    return [path for path in paths if path.startswith(SHIPPED)]


def unreleased(text: str) -> List[str]:
    """Returns the lines under the Unreleased heading, without the blank ones."""
    found: List[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith(VERSION):
            inside = line.startswith(UNRELEASED)
        elif inside and line.strip():
            found.append(line.strip())
    return found


def versions(text: str) -> Set[str]:
    return {line.strip() for line in text.splitlines() if line.startswith(VERSION)}


def has_entry(base: str, head: str) -> bool:
    """Says whether head adds a line under Unreleased, or a version heading as a release does when it moves the entries."""
    return bool(set(unreleased(head)) - set(unreleased(base)) or versions(head) - versions(base))


def read(path: Path) -> str:
    """Returns the file's text, or "" when it is missing: a changelog that was deleted or renamed holds no entry."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""


def labels(raw: str) -> List[str]:
    """Returns the label names in a JSON list. Anything else is no label."""
    try:
        found = json.loads(raw or "[]")
    except ValueError:
        return []
    return [name for name in found if isinstance(name, str)] if isinstance(found, list) else []


def main() -> int:
    missing = shipped(sys.stdin.buffer.read().decode("utf-8", "replace").split("\0"))
    if not missing:
        return 0
    base = read(Path(sys.argv[1])) if len(sys.argv) > 1 else ""
    if has_entry(base, read(Path(CHANGELOG))):
        return 0
    if LABEL in labels(os.environ.get("PR_LABELS", "")):
        print(f"No {CHANGELOG} entry, and the pull request carries the {LABEL} label.")
        return 0
    print(f"This pull request changes what the plugin ships and adds no entry under Unreleased in {CHANGELOG}:")
    for path in missing:
        print(f"  {path}")
    print(f"Add an entry, or add the {LABEL} label when the change alters nothing a user sees or Emet receives.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
