# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.3] - 2026-10-23

### Added

- Each turn is attributed to the person who ran it. The plugin sends the email the developer signs in to Claude with, read from Claude Code's account file, as `enduser_ref`, which Emet matches to a member of the organization. The new optional **Your Emet email** option (`emet_email`) overrides it, for an API key, Bedrock, Vertex AI or a gateway, or when the two emails differ. The email is sent in every capture mode, `metadata` included, and is reported by the developer's machine, not proven to Emet.
- `/emet:status` links the Emet report of the latest turn of the last five sessions, with their repository, and shows who turns are sent as, or why no email was found. `/emet:setup` gives the report link once a turn has been delivered. The link searches Emet's conversation list for the session, so it never opens another conversation.
- `docs/enterprise.md` describes a rollout where developers enter nothing: the team's key goes in the managed settings `env`, next to the Team ID and capture mode in `pluginConfigs`.

### Changed

- The key option is now `emet_key` (it was `api_key`). A key in managed settings `env` is passed to every plugin's hooks, so under the old name another plugin with its own `api_key` option would have taken the Emet key as its own. `env` values still reach every command Claude Code runs; `docs/enterprise.md` explains what that means and the keychain alternative. To update, run `claude plugin marketplace update emet-plugin` and `claude plugin update emet@emet-plugin`, restart Claude Code, then enter the key again with `/plugin configure emet@emet-plugin` (unless your administrator supplies it). Administrators who supply the key through managed settings replace `CLAUDE_PLUGIN_OPTION_API_KEY` with `CLAUDE_PLUGIN_OPTION_EMET_KEY` when they move the pin.
- In `full` mode Emet keys are now replaced with `[REDACTED:emet_key]` wherever they appear in what is sent; other content is still sent as is. With the key in managed `env`, a command that prints the environment would otherwise have sent it.
- The install prompt in the README updates the plugin when it is already installed, confirms that the Claude sign-in email is the Emet one (and asks for the Emet email when it is not, or there is no sign-in), and ends with `/emet:setup` in the new session.

## [0.1.2] - 2026-10-22

### Changed

- The Emet endpoint is no longer a plugin option. The plugin sends only to `https://app.emet.so` and refuses any other host, including one set through the environment; the only exception is an Emet on the same machine, for plugin development. An `endpoint` saved with 0.1.0 other than `https://app.emet.so` stops the plugin from sending until it is removed; `/emet:setup` says how. `/plugin configure` asks for the key, the Team ID and the capture mode.
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

[Unreleased]: https://github.com/FUSEis/emet-plugin/compare/v0.1.3...HEAD
[0.1.3]: https://github.com/FUSEis/emet-plugin/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/FUSEis/emet-plugin/compare/v0.1.0...v0.1.2
[0.1.0]: https://github.com/FUSEis/emet-plugin/releases/tag/v0.1.0
