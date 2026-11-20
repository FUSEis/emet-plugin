#!/usr/bin/env python3
"""Folds the changelog fragments into CHANGELOG.md under a new version heading.

    changelog_release.py [--check] <version> [<date>]

Run it from the root of the repository. It moves what sits under Unreleased, and the entry of
every file in changelog.d/, under `## [<version>] - <date>`, leaves Unreleased empty, updates
the links at the end of the file and deletes the fragments. With --check it prints the new
section and writes nothing. See CONTRIBUTING.md.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from changelog_check import CHANGELOG, FRAGMENTS, KINDS, README, UNRELEASED, VERSION, kind, load

SECTION = "### "
# The link definitions that end the file
LINK = re.compile(r"\[[^\]]+\]: ")
COMPARE = re.compile(r"\[Unreleased\]: (\S+)/compare/v(\S+)\.\.\.HEAD")
# ASCII digits, and no leading zero, as Semantic Versioning writes a version
PART = r"(?:0|[1-9][0-9]*)"
NUMBER = re.compile(rf"{PART}\.{PART}\.{PART}")
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class Refused(Exception):
    """A release that cannot be written. Its text says why."""


def title(name: str) -> str:
    return name.capitalize()


def bullets(text: str) -> str:
    """Returns a fragment's text as list items. A paragraph that is no item becomes one."""
    blocks = [block.splitlines() for block in re.split(r"\n\s*\n", text.strip())]
    out: List[str] = []
    for block in blocks:
        lines = [line.rstrip() for line in block]
        if lines[0][:1].isspace():
            # An indented paragraph goes on with the item above it
            out.append("")
        elif lines[0].startswith(("- ", "* ")):
            lines[0] = "- " + lines[0][2:]
        else:
            lines = ["- " + lines[0]] + ["  " + line.lstrip() for line in lines[1:]]
        out.extend(lines)
    return "\n".join(out)


def collect(folder: Path) -> List[Tuple[str, str]]:
    """Returns the name and the text of every fragment, sorted by name."""
    found: List[Tuple[str, str]] = []
    if folder.is_symlink():
        raise Refused(f"{FRAGMENTS} is a symbolic link")
    if not folder.is_dir():
        return found
    for path in sorted(folder.iterdir(), key=lambda path: path.name):
        if path.name == README:
            continue
        wrong, text = load(path.name, path)
        if wrong:
            raise Refused(f"{FRAGMENTS}{path.name} is not a well-formed fragment: {wrong}")
        found.append((path.name, text))
    return found


def parts(text: str) -> Tuple[List[str], List[str], List[str]]:
    """Returns the lines up to and with the Unreleased heading, the lines under it, and the rest."""
    lines = text.splitlines()
    starts = [number for number, line in enumerate(lines) if line.rstrip() == UNRELEASED]
    if len(starts) != 1:
        raise Refused(f"{CHANGELOG} must hold the heading {UNRELEASED} once")
    start = starts[0] + 1
    end = start
    while end < len(lines) and not lines[end].startswith(VERSION) and not LINK.match(lines[end]):
        end += 1
    return lines[:start], lines[start:end], lines[end:]


def sections(lines: Sequence[str]) -> Tuple[str, Dict[str, str]]:
    """Returns the text above the first heading under Unreleased, and the text under each heading."""
    found: Dict[str, List[str]] = {"": []}
    current = ""
    for line in lines:
        if line.startswith(SECTION):
            current = line[len(SECTION) :].strip()
            if current not in map(title, KINDS):
                raise Refused(
                    f"{CHANGELOG} has the heading '{line.strip()}' under Unreleased, which is none of {', '.join(map(title, KINDS))}"
                )
            found.setdefault(current, [])
        else:
            found[current].append(line.rstrip())
    texts = {name: "\n".join(body).strip("\n") for name, body in found.items()}
    return texts.pop(""), texts


def linked(rest: Sequence[str], version: str) -> List[str]:
    """Returns the rest of the file with Unreleased compared against the new version, and a link for that version."""
    out: List[str] = []
    for line in rest:
        match = COMPARE.fullmatch(line.strip())
        if match:
            url, last = match.groups()
            out.append(f"[Unreleased]: {url}/compare/v{version}...HEAD")
            out.append(f"[{version}]: {url}/compare/v{last}...v{version}")
        else:
            out.append(line)
    return out


def build(text: str, fragments: Sequence[Tuple[str, str]], version: str, date: str) -> Tuple[str, str]:
    """Returns the new changelog and, on its own, the section of the new version."""
    if any(line.startswith(f"{VERSION}{version}]") for line in text.splitlines()):
        raise Refused(f"{CHANGELOG} already has a heading for {version}")
    before, body, rest = parts(text)
    above, entries = sections(body)
    blocks = [above] if above else []
    for name in KINDS:
        texts = [entries.get(title(name), "")] + [bullets(entry) for file, entry in fragments if kind(file) == name]
        texts = [entry for entry in texts if entry]
        if texts:
            blocks.append(f"{SECTION}{title(name)}\n\n" + "\n".join(texts))
    if not blocks:
        raise Refused(f"nothing to release: no entry under Unreleased and no fragment in {FRAGMENTS}")
    section = "\n\n".join(blocks) + "\n"
    out = [*before, "", f"{VERSION}{version}] - {date}", "", section]
    return "\n".join(out) + "".join(f"{line}\n" for line in ["", *linked(rest, version)] if rest), section


def replace(name: str, text: str) -> None:
    """Writes the file whole or not at all: a write that fails leaves the one that was there."""
    fd, temp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(name)), prefix=f".{name}.", suffix=".tmp")
    try:
        # newline: the file keeps its line feeds on Windows
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
        os.chmod(temp, os.stat(name).st_mode & 0o777)
        os.replace(temp, name)
    except BaseException:
        os.unlink(temp)
        raise


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Folds the changelog fragments into CHANGELOG.md under a new version.")
    parser.add_argument("version", help="the new version, as in 0.1.5")
    parser.add_argument("date", nargs="?", default=datetime.date.today().isoformat(), help="the release date, today when left out")
    parser.add_argument("--check", action="store_true", help="print the new section and write nothing")
    args = parser.parse_args(argv)
    try:
        if not NUMBER.fullmatch(args.version):
            raise Refused(f"'{args.version}' is no version of the form 1.2.3")
        try:
            if not DATE.fullmatch(args.date):
                raise ValueError
            datetime.datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            raise Refused(f"'{args.date}' is no date of the form 2026-01-31") from None
        try:
            text = Path(CHANGELOG).read_text(encoding="utf-8")
        except (OSError, ValueError):
            raise Refused(f"{CHANGELOG} cannot be read; run this from the root of the repository") from None
        fragments = collect(Path(FRAGMENTS))
        changelog, section = build(text, fragments, args.version, args.date)
    except Refused as refused:
        print(f"No release written: {refused}.", file=sys.stderr)
        return 1
    if args.check:
        sys.stdout.write(section)
        return 0
    try:
        replace(CHANGELOG, changelog)
    except OSError as error:
        print(f"No release written: {CHANGELOG} could not be replaced ({type(error).__name__}). It is as it was.", file=sys.stderr)
        return 1
    remain: List[str] = []
    for name, _ in fragments:
        try:
            (Path(FRAGMENTS) / name).unlink()
        except OSError:
            remain.append(f"{FRAGMENTS}{name}")
    if remain:
        print(f"{CHANGELOG} has {args.version}, and these fragments could not be deleted:", file=sys.stderr)
        print("\n".join(f"  {path}" for path in remain), file=sys.stderr)
        print("Delete them by hand to finish the release. Do not run the release again.", file=sys.stderr)
        return 1
    print(f"{CHANGELOG} has {args.version} with {len(fragments)} fragment(s) folded in. Their files are deleted.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
