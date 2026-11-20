#!/usr/bin/env python3
"""Fails when a pull request changes what the plugin ships and adds no changelog entry.

An entry is a new fragment file in changelog.d/. A new line under Unreleased in CHANGELOG.md
and a new version heading count too. A fragment that is not well formed fails on its own.

Reads the changed paths from stdin, separated by NUL as `git diff --name-only -z` writes them,
and the pull request's labels from PR_LABELS as a JSON list. The first argument is a file holding
CHANGELOG.md as the base branch has it, the second a file holding the paths the pull request
adds under changelog.d/, separated by NUL. The pull request's own files are read from the
current folder. See CONTRIBUTING.md.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Set

# What Claude Code installs and runs: the hook script, the hooks, the skills and the manifest. Not the plugin's README
SHIPPED = ("claude/scripts/", "claude/hooks/", "claude/skills/", "claude/.claude-plugin/")
CHANGELOG = "CHANGELOG.md"
UNRELEASED = "## [Unreleased]"
VERSION = "## ["
# The label of a pull request whose change needs no entry
LABEL = "no-changelog"
# One file per pull request, folded into CHANGELOG.md at release by changelog_release.py
FRAGMENTS = "changelog.d/"
README = "README.md"
# The Keep a Changelog headings, in the order a release writes them
KINDS = ("added", "changed", "deprecated", "removed", "fixed", "security")
# Lower case only: two names that differ by case are one file on macOS and Windows
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\.([a-z]+)\.md")
NAMING = f"<slug>.<kind>.md, the slug in lower case letters, digits and hyphens, the kind one of {', '.join(KINDS)}"


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


def kind(name: str) -> Optional[str]:
    """Returns the kind a fragment's file name gives, or None when the name is not a fragment's."""
    match = NAME.fullmatch(name)
    return match.group(1) if match and match.group(1) in KINDS else None


def problem(name: str, path: Path) -> str:
    """Says what is wrong with the fragment at path, or "" when it is well formed."""
    if kind(name) is None:
        return f"its name is not {NAMING}"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return "it is not a readable UTF-8 text file"
    if not text.strip():
        return "it is empty"
    # A heading in an entry would end the version's section where the release notes are cut
    if any(line.startswith("#") for line in text.splitlines()):
        return "a line starts with #, and an entry holds no heading"
    return ""


def fragments(paths: Iterable[str]) -> List[str]:
    """Returns the paths in the fragment folder, without its README."""
    return [path for path in paths if path.startswith(FRAGMENTS) and path != FRAGMENTS + README]


def malformed(paths: Iterable[str]) -> List[str]:
    """Returns one line for each fragment among the paths that is there and is not well formed."""
    found: List[str] = []
    for path in fragments(paths):
        # A fragment a release deleted is gone, and nothing is left to check
        if Path(path).exists():
            wrong = problem(path[len(FRAGMENTS) :], Path(path))
            if wrong:
                found.append(f"  {path}: {wrong}")
    return found


def split(raw: bytes) -> List[str]:
    return [path for path in raw.decode("utf-8", "replace").split("\0") if path]


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
    changed = split(sys.stdin.buffer.read())
    wrong = malformed(changed)
    if wrong:
        print(f"A file in {FRAGMENTS} is not a well-formed changelog fragment:")
        print("\n".join(wrong))
        print(f"A fragment is named {NAMING}. It holds the entry's text.")
        return 1
    missing = shipped(changed)
    if not missing:
        return 0
    added = split(Path(sys.argv[2]).read_bytes()) if len(sys.argv) > 2 else []
    if fragments(added):
        return 0
    base = read(Path(sys.argv[1])) if len(sys.argv) > 1 else ""
    if has_entry(base, read(Path(CHANGELOG))):
        return 0
    if LABEL in labels(os.environ.get("PR_LABELS", "")):
        print(f"No {CHANGELOG} entry, and the pull request carries the {LABEL} label.")
        return 0
    print(f"This pull request changes what the plugin ships and adds no changelog fragment in {FRAGMENTS}:")
    for path in missing:
        print(f"  {path}")
    print(f"Add a file named {NAMING}.")
    print(f"Or add the {LABEL} label when the change alters nothing a user sees or Emet receives.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
