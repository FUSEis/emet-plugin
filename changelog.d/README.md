# Changelog fragments

Each pull request that needs a changelog entry adds one file here. No two pull requests edit the same file, so their entries never conflict. At release, `.github/scripts/changelog_release.py` moves every entry into `CHANGELOG.md` and deletes the files.

## The file

Name it `<slug>.<kind>.md`.

- The slug says what the change is, in lower case letters, digits and hyphens. Make it specific, so no other pull request picks the same name. It needs no pull request number. A slug that is a device name on Windows, such as `con`, `aux` or `nul`, is refused.
- The kind is one of `added`, `changed`, `deprecated`, `removed`, `fixed`, `security`. It decides the heading the entry goes under.

The file holds the entry's text, written for a user of the plugin, as it should read in `CHANGELOG.md`. A plain paragraph becomes one list item. Start lines with `- ` to write several. Use no headings.

Example, `changelog.d/skip-unreadable-turn.fixed.md`:

```markdown
A turn that cannot be read no longer stops the session from being sent. It is skipped and written to the log as `turn skipped`.
```

A change with entries of two kinds adds two files.

## Rules CI checks

- A pull request that changes what the plugin ships adds a file here, or carries the `no-changelog` label.
- Every file here except this one is a well-formed fragment: the name as above, a regular file and no link, text inside, no line that starts with `#`.

See [CONTRIBUTING.md](../CONTRIBUTING.md) for when a change needs an entry.
