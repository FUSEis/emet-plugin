# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- The Emet endpoint is no longer a plugin option. The plugin sends only to `https://app.emet.so` and refuses any other host, including one set through the environment. An `endpoint` saved with 0.1.0 other than `https://app.emet.so` stops the plugin from sending until it is removed; `/emet:setup` says how. `/plugin configure` asks for the key, the Team ID and the capture mode.
- The install prompt in the README and `/emet:setup` say where each value is in Emet, that only the key is left to set after install, and to run `/emet:setup` again after the restart. `/emet:setup` also explains that an accepted key does not check the Team ID, and how to confirm the team.

### Fixed

- Where the key is kept: Claude Code's secure credential store. It was described as always the system keychain.
- The first model call of a turn or subagent now starts when its prompt was sent, so its duration includes the wait for the first response. It used to start at the first response.
- Model spans now report how many secrets were redacted from the text they wrote, and the turn span reports those redacted from the prompt. Tool spans still count their arguments and result, so each redaction is counted once in a trace. Before, only tool spans reported a count.

## [0.1.0] - 2026-10-20

First release of the Claude Code plugin.

### Added

- Sends each finished turn to Emet as one trace: the prompt, each model call with token counts, each tool call with its arguments and result, and each subagent with its own calls.
- Capture modes: `sanitized` (default, secrets redacted), `full` and `metadata` (no content).
- Images and other binary content are replaced by a placeholder in every mode, and the span is marked truncated.
- Tool output that Claude Code saved to disk because it was too large is sent in full.
- Background subagents that finish after their turn are added to that turn.
- Large turns are split into batches that fit Emet's limits.
- Undelivered turns are queued and retried in order, with a bounded queue.
- `SessionStart` checks that Emet accepts the key, without sending a trace.
- `/emet:setup` and `/emet:status` skills.
- Managed-settings guide for organization rollouts.

[Unreleased]: https://github.com/FUSEis/emet-plugin/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/FUSEis/emet-plugin/releases/tag/v0.1.0
