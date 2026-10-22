# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- The Emet endpoint is no longer a plugin option. The plugin sends only to `https://app.emet.so`, and `/plugin configure` asks for the key, the Team ID and the capture mode.
- `/emet:setup` and the install prompt in the README name each option, say where the Team ID comes from (the External ID on Emet's Teams page), and explain that only the key is left to set after install.

### Fixed

- Where the key is kept: the macOS Keychain, or Claude Code's credentials file where there is no keychain. It was described as always the system keychain.

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
