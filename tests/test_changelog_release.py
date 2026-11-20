"""The release script that folds the fragments in changelog.d/ into CHANGELOG.md."""

import os
import re
import subprocess
import sys
from pathlib import Path

import changelog_release
import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "changelog_release.py"

CHANGELOG = """# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Changed

- A change already under Unreleased.

### Added

- An addition already under Unreleased.

## [0.1.4] - 2026-11-19

### Fixed

- Released.

[Unreleased]: https://github.com/FUSEis/emet-plugin/compare/v0.1.4...HEAD
[0.1.4]: https://github.com/FUSEis/emet-plugin/compare/v0.1.3...v0.1.4
"""
EMPTY = CHANGELOG.replace(
    "### Changed\n\n- A change already under Unreleased.\n\n### Added\n\n- An addition already under Unreleased.\n\n", ""
)

FRAGMENTS = {
    "zebra.fixed.md": "A fix, named last.\n",
    "apple.fixed.md": "- A fix, named first.\n",
    "wake.security.md": "A security entry.\n",
    "old-flag.removed.md": "A removal.\n",
    "old-name.deprecated.md": "A deprecation.\n",
    "quiet.changed.md": "A new change.\n",
    "icon.added.md": "A new addition.\n",
}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "CHANGELOG.md").write_bytes(CHANGELOG.encode())
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "changelog.d" / "README.md").write_text("# Changelog fragments\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def fragments(repo, files):
    for name, text in files.items():
        (repo / "changelog.d" / name).write_bytes(text if isinstance(text, bytes) else text.encode())


def changelog(repo):
    return (repo / "CHANGELOG.md").read_bytes().decode()


def left(repo):
    return sorted(path.name for path in (repo / "changelog.d").iterdir())


def release_notes(text, version):
    """Cuts a version's section out of the changelog as the awk line in release.yml does."""
    found, notes = False, []
    for line in text.splitlines():
        if re.search(r"^## \[" + version + r"\]", line):
            found = True
        elif found and re.search(r"^## \[", line):
            break
        elif found:
            notes.append(line)
    return "\n".join(notes)


def test_the_release_workflow_cuts_the_notes_the_way_these_tests_do():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert (
        """awk -v v="${TAG#v}" '$0 ~ "^## \\\\[" v "\\\\]" {found=1; next} found && /^## \\[/ {exit} found' CHANGELOG.md > notes.md"""
        in workflow
    )
    assert release_notes(CHANGELOG, "0.1.4").strip().startswith("### Fixed\n\n- Released.")


def test_a_release_writes_the_fragments_and_the_unreleased_entries_under_the_new_version(repo):
    fragments(repo, FRAGMENTS)
    assert changelog_release.main(["0.1.5", "2026-12-01"]) == 0
    assert (
        changelog(repo)
        == """# Changelog

All notable changes to this project are documented here.

## [Unreleased]

## [0.1.5] - 2026-12-01

### Added

- An addition already under Unreleased.
- A new addition.

### Changed

- A change already under Unreleased.
- A new change.

### Deprecated

- A deprecation.

### Removed

- A removal.

### Fixed

- A fix, named first.
- A fix, named last.

### Security

- A security entry.

## [0.1.4] - 2026-11-19

### Fixed

- Released.

[Unreleased]: https://github.com/FUSEis/emet-plugin/compare/v0.1.5...HEAD
[0.1.5]: https://github.com/FUSEis/emet-plugin/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/FUSEis/emet-plugin/compare/v0.1.3...v0.1.4
"""
    )


def test_a_release_deletes_the_fragments_and_keeps_the_readme(repo):
    fragments(repo, FRAGMENTS)
    assert changelog_release.main(["0.1.5", "2026-12-01"]) == 0
    assert left(repo) == ["README.md"]


def test_the_release_workflow_still_finds_the_new_section_and_the_one_before_it(repo):
    fragments(repo, FRAGMENTS)
    assert changelog_release.main(["0.1.5", "2026-12-01"]) == 0
    notes = release_notes(changelog(repo), "0.1.5")
    assert notes.strip().startswith("### Added\n\n- An addition already under Unreleased.\n- A new addition.")
    assert notes.strip().endswith("### Security\n\n- A security entry.")
    assert "Released" not in notes and "compare" not in notes
    assert release_notes(changelog(repo), "0.1.4").strip().startswith("### Fixed\n\n- Released.")
    assert release_notes(changelog(repo), "Unreleased").strip() == ""


def test_a_release_with_no_fragments_moves_the_unreleased_entries(repo):
    assert changelog_release.main(["0.1.5", "2026-12-01"]) == 0
    assert (
        "## [Unreleased]\n\n## [0.1.5] - 2026-12-01\n\n### Added\n\n- An addition already under Unreleased.\n\n### Changed\n\n"
        in changelog(repo)
    )
    assert "- A change already under Unreleased.\n\n## [0.1.4] - 2026-11-19\n" in changelog(repo)


def test_a_release_with_no_fragment_folder_at_all_still_works(repo):
    (repo / "changelog.d" / "README.md").unlink()
    (repo / "changelog.d").rmdir()
    assert changelog_release.main(["0.1.5", "2026-12-01"]) == 0
    assert "## [0.1.5] - 2026-12-01\n\n### Added\n" in changelog(repo)


def test_a_release_with_fragments_only_gets_only_their_headings(repo):
    (repo / "CHANGELOG.md").write_bytes(EMPTY.encode())
    fragments(repo, {"apple.fixed.md": "A fix.\n"})
    assert changelog_release.main(["0.1.5", "2026-12-01"]) == 0
    assert "## [Unreleased]\n\n## [0.1.5] - 2026-12-01\n\n### Fixed\n\n- A fix.\n\n## [0.1.4] - 2026-11-19\n" in changelog(repo)


def test_a_release_is_the_same_whatever_order_the_folder_lists_its_files_in(repo, monkeypatch):
    fragments(repo, FRAGMENTS)
    listed = sorted((repo / "changelog.d").iterdir())
    monkeypatch.setattr(Path, "iterdir", lambda self: iter(listed))
    assert changelog_release.main(["--check", "0.1.5", "2026-12-01"]) == 0
    monkeypatch.setattr(Path, "iterdir", lambda self: iter(reversed(listed)))
    assert changelog_release.main(["0.1.5", "2026-12-01"]) == 0
    assert "- A fix, named first.\n- A fix, named last.\n" in changelog(repo)


@pytest.mark.parametrize(
    ("text", "written"),
    [
        ("One paragraph.\n", "- One paragraph."),
        ("\n\nOne paragraph,  \nover two lines.\n\n", "- One paragraph,\n  over two lines."),
        ("First paragraph.\n\nSecond paragraph.\n", "- First paragraph.\n- Second paragraph."),
        ("- First item.\n- Second item.\n", "- First item.\n- Second item."),
        ("* A star item.\n", "- A star item."),
        ("- An item.\n  - With one below it.\n", "- An item.\n  - With one below it."),
        ("- An item.\n\n  That goes on.\n\nAnd a paragraph.\n", "- An item.\n\n  That goes on.\n- And a paragraph."),
        ("Windows line ends.\r\n\r\nTwice.\r\n", "- Windows line ends.\n- Twice."),
    ],
)
def test_a_paragraph_becomes_a_list_item_and_list_items_stay_as_written(repo, text, written):
    (repo / "CHANGELOG.md").write_bytes(EMPTY.encode())
    fragments(repo, {"apple.fixed.md": text.encode()})
    assert changelog_release.main(["0.1.5", "2026-12-01"]) == 0
    assert f"### Fixed\n\n{written}\n\n## [0.1.4]" in changelog(repo)
    assert "\r" not in changelog(repo)


def test_the_date_is_today_when_none_is_given(repo, monkeypatch):
    class Day(changelog_release.datetime.date):
        @classmethod
        def today(cls):
            return cls(2027, 1, 31)

    monkeypatch.setattr(changelog_release.datetime, "date", Day)
    assert changelog_release.main(["0.1.5"]) == 0
    assert "\n## [0.1.5] - 2027-01-31\n" in changelog(repo)


def refused(repo, capsys, arguments, reason):
    before, files = changelog(repo), left(repo)
    assert changelog_release.main(arguments) == 1
    captured = capsys.readouterr()
    assert reason in captured.err and captured.out == ""
    assert changelog(repo) == before and left(repo) == files


@pytest.mark.parametrize("version", ["0.1.4", "0.1.3"])
def test_a_version_that_already_has_a_heading_is_refused_and_nothing_is_touched(repo, capsys, version):
    (repo / "CHANGELOG.md").write_bytes(CHANGELOG.replace("## [0.1.4]", "## [0.1.4] - 2026-11-19\n\n## [0.1.3]").encode())
    fragments(repo, FRAGMENTS)
    refused(repo, capsys, [version, "2026-12-01"], f"CHANGELOG.md already has a heading for {version}")


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        (["v0.1.5", "2026-12-01"], "'v0.1.5' is no version of the form 1.2.3"),
        (["0.1", "2026-12-01"], "'0.1' is no version of the form 1.2.3"),
        (["Unreleased", "2026-12-01"], "is no version"),
        # A leading zero, and digits that are not ASCII
        (["01.1.5", "2026-12-01"], "'01.1.5' is no version of the form 1.2.3"),
        (["0.1.05", "2026-12-01"], "is no version"),
        (["0.00.5", "2026-12-01"], "is no version"),
        (["0.1.\u0665", "2026-12-01"], "is no version"),
        (["0.1.5\n", "2026-12-01"], "is no version"),
        # A real date that is not padded with zeros
        (["0.1.5", "2026-12-1"], "'2026-12-1' is no date of the form 2026-01-31"),
        (["0.1.5", "2026-1-01"], "is no date"),
        (["0.1.5", "26-12-01"], "is no date"),
        (["0.1.5", "2026-12-0\u0661"], "is no date"),
        (["0.1.5", "2026-12-01\n"], "is no date"),
        (["0.1.5", "01/12/2026"], "'01/12/2026' is no date of the form 2026-01-31"),
        (["0.1.5", "2026-02-30"], "is no date"),
    ],
)
def test_a_version_or_a_date_of_another_form_is_refused(repo, capsys, arguments, reason):
    fragments(repo, FRAGMENTS)
    refused(repo, capsys, arguments, reason)


@pytest.mark.parametrize(
    ("name", "text", "reason"),
    [
        ("apple.fix.md", "Text.\n", "changelog.d/apple.fix.md is not a well-formed fragment: its name is not"),
        ("apple.fixed.md", "", "changelog.d/apple.fixed.md is not a well-formed fragment: it is empty"),
        ("apple.fixed.md", "## [9.9.9]\n", "a line starts with #"),
        ("notes.txt", "Text.\n", "changelog.d/notes.txt is not a well-formed fragment"),
    ],
)
def test_a_malformed_fragment_stops_the_release_and_is_named(repo, capsys, name, text, reason):
    fragments(repo, {**FRAGMENTS, name: text})
    refused(repo, capsys, ["0.1.5", "2026-12-01"], reason)


@pytest.mark.parametrize("version", ["0.0.0", "10.20.30", "1.0.100"])
def test_a_version_with_a_zero_part_or_long_parts_is_released(repo, version):
    assert changelog_release.main([version, "2026-12-01"]) == 0
    assert f"\n## [{version}] - 2026-12-01\n" in changelog(repo)


links = pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")


@links
@pytest.mark.parametrize("target", ["outside", "nothing-there"])
def test_a_fragment_that_is_a_link_stops_the_release_and_its_target_is_never_read(repo, capsys, tmp_path_factory, target):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "outside").write_text("Not for the changelog.\n", encoding="utf-8")
    fragments(repo, FRAGMENTS)
    (repo / "changelog.d" / "linked.fixed.md").symlink_to(outside / target)
    refused(repo, capsys, ["0.1.5", "2026-12-01"], "changelog.d/linked.fixed.md is not a well-formed fragment: it is a symbolic link")
    assert changelog_release.main(["--check", "0.1.5", "2026-12-01"]) == 1
    assert "Not for the changelog" not in capsys.readouterr().out


@links
def test_a_fragment_folder_that_is_a_link_stops_the_release(repo, capsys, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    (elsewhere / "apple.fixed.md").write_text("From another folder.\n", encoding="utf-8")
    (repo / "changelog.d" / "README.md").unlink()
    (repo / "changelog.d").rmdir()
    (repo / "changelog.d").symlink_to(elsewhere, target_is_directory=True)
    refused(repo, capsys, ["0.1.5", "2026-12-01"], "changelog.d/ is a symbolic link")
    assert (elsewhere / "apple.fixed.md").exists()


def test_a_write_that_fails_leaves_the_changelog_and_the_fragments_as_they_were(repo, capsys, monkeypatch):
    fragments(repo, FRAGMENTS)
    before, files, folder = changelog(repo), left(repo), sorted(path.name for path in repo.iterdir())

    def fail(source, target):
        # The new text is whole in a file beside the changelog by now, and the changelog is untouched
        assert Path(source).parent == repo and "## [0.1.5] - 2026-12-01" in Path(source).read_text(encoding="utf-8")
        assert changelog(repo) == before
        raise PermissionError("no")

    monkeypatch.setattr(changelog_release.os, "replace", fail)
    assert changelog_release.main(["0.1.5", "2026-12-01"]) == 1
    assert "No release written: CHANGELOG.md could not be replaced (PermissionError). It is as it was." in capsys.readouterr().err
    assert changelog(repo) == before and left(repo) == files
    # The temporary file is gone too
    assert sorted(path.name for path in repo.iterdir()) == folder


def test_the_changelog_is_never_opened_for_writing_in_place(repo, monkeypatch):
    fragments(repo, FRAGMENTS)
    opened = []
    real = changelog_release.os.fdopen
    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: opened.append(args) or pytest.fail("opened in place"))
    monkeypatch.setattr(changelog_release.os, "fdopen", lambda fd, *args, **kwargs: opened.append(fd) or real(fd, *args, **kwargs))
    assert changelog_release.main(["0.1.5", "2026-12-01"]) == 0
    monkeypatch.undo()
    assert len(opened) > 1 and "\n## [0.1.5] - 2026-12-01\n" in changelog(repo)


@pytest.mark.skipif(os.name == "nt", reason="Windows keeps no such access bits")
def test_the_new_changelog_keeps_the_access_bits_of_the_old_one(repo):
    os.chmod(repo / "CHANGELOG.md", 0o644)
    assert changelog_release.main(["0.1.5", "2026-12-01"]) == 0
    assert (repo / "CHANGELOG.md").stat().st_mode & 0o777 == 0o644


def test_a_fragment_that_cannot_be_deleted_is_named_and_the_rest_are_deleted(repo, capsys, monkeypatch):
    fragments(repo, FRAGMENTS)
    real = Path.unlink

    def unlink(self, *args, **kwargs):
        if self.name in ("apple.fixed.md", "quiet.changed.md"):
            raise PermissionError("no")
        real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    assert changelog_release.main(["0.1.5", "2026-12-01"]) == 1
    monkeypatch.undo()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        "CHANGELOG.md has 0.1.5, and these fragments could not be deleted:\n  changelog.d/apple.fixed.md\n  changelog.d/quiet.changed.md\n"
        in captured.err
    )
    assert "Delete them by hand to finish the release. Do not run the release again." in captured.err
    assert "- A fix, named first.\n" in changelog(repo)
    assert left(repo) == ["README.md", "apple.fixed.md", "quiet.changed.md"]


def test_a_release_of_nothing_is_refused(repo, capsys):
    (repo / "CHANGELOG.md").write_bytes(EMPTY.encode())
    refused(repo, capsys, ["0.1.5", "2026-12-01"], "nothing to release")


@pytest.mark.parametrize(
    ("old", "new", "reason"),
    [
        ("## [Unreleased]\n", "", "CHANGELOG.md must hold the heading ## [Unreleased] once"),
        ("## [0.1.4] - 2026-11-19\n", "## [Unreleased]\n", "CHANGELOG.md must hold the heading ## [Unreleased] once"),
        ("### Changed\n", "### Improved\n", "the heading '### Improved' under Unreleased"),
    ],
)
def test_a_changelog_the_script_cannot_place_entries_in_is_refused(repo, capsys, old, new, reason):
    (repo / "CHANGELOG.md").write_bytes(CHANGELOG.replace(old, new).encode())
    fragments(repo, FRAGMENTS)
    refused(repo, capsys, ["0.1.5", "2026-12-01"], reason)


def test_a_folder_with_no_changelog_is_refused(repo, capsys):
    (repo / "CHANGELOG.md").unlink()
    assert changelog_release.main(["0.1.5", "2026-12-01"]) == 1
    assert "CHANGELOG.md cannot be read" in capsys.readouterr().err
    assert not (repo / "CHANGELOG.md").exists()


def test_check_prints_the_new_section_and_writes_nothing(repo, capsys):
    fragments(repo, FRAGMENTS)
    before, files = changelog(repo), left(repo)
    assert changelog_release.main(["--check", "0.1.5", "2026-12-01"]) == 0
    out = capsys.readouterr().out
    assert changelog(repo) == before and left(repo) == files
    assert changelog_release.main(["0.1.5", "2026-12-01"]) == 0
    assert out == release_notes(changelog(repo), "0.1.5").strip("\n") + "\n"
    assert out.startswith("### Added\n\n- An addition already under Unreleased.\n- A new addition.\n")


def test_text_above_the_first_heading_under_unreleased_is_kept_at_the_top(repo):
    (repo / "CHANGELOG.md").write_bytes(CHANGELOG.replace("## [Unreleased]\n", "## [Unreleased]\n\nA note on this release.\n").encode())
    assert changelog_release.main(["0.1.5", "2026-12-01"]) == 0
    assert "## [Unreleased]\n\n## [0.1.5] - 2026-12-01\n\nA note on this release.\n\n### Added\n" in changelog(repo)


def test_a_changelog_with_no_release_yet_and_no_links_gets_its_first_version(repo):
    (repo / "CHANGELOG.md").write_bytes(b"# Changelog\n\n## [Unreleased]\n")
    fragments(repo, {"first.added.md": "The first entry.\n"})
    assert changelog_release.main(["0.1.0", "2026-12-01"]) == 0
    assert changelog(repo) == "# Changelog\n\n## [Unreleased]\n\n## [0.1.0] - 2026-12-01\n\n### Added\n\n- The first entry.\n"


def test_the_links_at_the_end_are_no_entries_when_no_version_stands_before_them(repo):
    text = "# Changelog\n\n## [Unreleased]\n\n### Fixed\n\n- A fix.\n\n[Unreleased]: https://example.invalid/compare/v0.0.1...HEAD\n"
    (repo / "CHANGELOG.md").write_bytes(text.encode())
    assert changelog_release.main(["0.1.0", "2026-12-01"]) == 0
    assert changelog(repo).endswith(
        "### Fixed\n\n- A fix.\n\n[Unreleased]: https://example.invalid/compare/v0.1.0...HEAD\n"
        "[0.1.0]: https://example.invalid/compare/v0.0.1...v0.1.0\n"
    )


def test_the_script_runs_from_the_command_line_as_the_release_steps_call_it(repo):
    fragments(repo, FRAGMENTS)
    done = subprocess.run([sys.executable, str(SCRIPT), "0.1.5", "2026-12-01"], cwd=repo, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "7 fragment(s)" in done.stdout
    assert "\n## [0.1.5] - 2026-12-01\n\n### Added\n" in changelog(repo) and left(repo) == ["README.md"]
    guide = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "`uv run python .github/scripts/changelog_release.py X.Y.Z`" in guide and "`--check`" in guide


def test_this_repositorys_own_changelog_and_fragments_make_a_release():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    entries = changelog_release.collect(ROOT / "changelog.d")
    try:
        built, section = changelog_release.build(text, entries, "99.0.0", "2026-12-01")
    except changelog_release.Refused as refused:
        # Right after a release nothing waits to be released
        assert "nothing to release" in str(refused)
        return
    assert release_notes(built, "99.0.0").strip("\n") + "\n" == section
    for line in text.splitlines():
        if line.startswith("- "):
            assert line in built.splitlines()
