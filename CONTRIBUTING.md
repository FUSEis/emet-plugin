# Contributing

## Development

The plugin runs on the Python standard library alone, so it works on any machine with `python3` 3.9 or later. The tests and linters use [uv](https://docs.astral.sh/uv/):

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
claude plugin validate --strict ./claude && claude plugin validate --strict .
```

To try a change in a real session, load the plugin from your checkout and give it options through the environment that Claude Code would otherwise set. `CLAUDE_PLUGIN_OPTION_ENDPOINT` is not a plugin option; it points the hook at an Emet on this machine for development, and the hook refuses any other host:

```bash
CLAUDE_PLUGIN_OPTION_EMET_KEY=... CLAUDE_PLUGIN_OPTION_TENANT_REF=... \
CLAUDE_PLUGIN_OPTION_ENDPOINT=http://localhost:8000 \
claude --plugin-dir ./claude
```

The hook log is in the plugin's data folder (`~/.claude/plugins/data/emet-inline/emet.log` with `--plugin-dir`).

## Rules for changes

- **No runtime dependencies.** Anything outside the standard library would have to be installed on every developer machine.
- **Python 3.9 compatible.** It is what macOS ships. CI runs the tests on 3.9 and on the latest release.
- **Never block the session.** Hooks return at once; all network work happens in the detached worker, and every failure there is logged, not raised.
- **Never log content or the key.** The log holds ids, counts and HTTP statuses only; the status file also holds report links and the email sent. A test checks this.
- **Say when content is cut.** Any tool result that is not sent complete is marked `is_truncated`, which Emet needs to avoid verifying a claim against partial evidence.
- **Tests use synthetic transcripts** built in `tests/helpers.py`. Never commit a real session transcript; they hold private content.
- A change to what is sent, or to the transcript parsing, needs a test and a `CHANGELOG.md` entry under **Unreleased**.

## Pull requests

`main` is protected: every change goes through a pull request with passing checks, and is squash-merged. Keep pull requests to one change, and describe what changes for users and for the data that is sent.

## Releases

1. Move the **Unreleased** entries in `CHANGELOG.md` under the new version.
2. Set the version in `claude/.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `claude/scripts/emet_claude/__init__.py` and `pyproject.toml`, and the `ref` pins in `docs/enterprise.md`, then run `uv lock` (tests keep them equal).
3. Merge, then push a tag `vX.Y.Z` on `main`. The release workflow checks the version and publishes the GitHub release with the changelog entry.
