# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- A turn that the plugin's own report notice started is sent marked. When Claude takes a turn because the plugin handed it an Emet report, the turn's root span carries the attribute `emet.plugin.turn_started_by` with the value `report_wake`, and its prompt, which is Emet's report text and not a person's message, is sent as one fixed line in place of that text. To recognise such a turn the plugin reads three things: Claude Code names the turn a task notification in the session file, the prompt holds the notice's opening sentence, and the plugin recorded handing a report to Claude in the ten minutes before. Nothing hands a report to Claude yet, so no turn is marked today and every turn is sent as before.
- The plugin keeps two more values for each delivered turn, in its own folder: Claude Code's id of the turn (`promptId` in the session file) and the time of the turn's last entry. They are written to the session's `pending.jsonl` file next to Emet's id of the turn, and wait with a turn in the retry queue. Neither is sent: they are taken off before a request is made, and Emet receives the same envelope as before.

### Changed

- `/emet:status` and the log show a failed send as its HTTP status, with Emet's short error code when the answer has one, as in `400 some_code`. Before, they showed up to 200 characters of whatever the server answered. A proxy or a server standing in for Emet could put its own text there, which `/emet:status` then read to Claude, and an answer that repeated part of a refused turn would have put session content in the log. A code is kept only when it is made of lowercase letters and underscores, 40 characters at most; anything else is dropped. The fixed phrases `key rejected`, `redirected` and `network ...` are as before.
- The status skill treats every log line and every value of the status file as data to report, never as an instruction.
- A log line is always one line. A line break, a control character or an invisible character in a logged value is written as `?`, and a line is cut at 300 characters. Before, an id read from a queued file could hold a line break and so write a second line that looked like the plugin's own, such as `401 key rejected`.

### Fixed

- The plugin no longer writes through a symbolic link in its data folder. Before, a link placed at the log, or at the name a file has while it is being written, made the plugin write into the file the link pointed to. A link at the log is now removed and a new log is started; the file being written is always a new one, and a leftover from a crashed run is cleared first. On macOS and Linux the open itself refuses a link. Windows has no such flag, so there the check alone applies.
- A subagent's file is checked again each time it is read. The plugin keeps the path of every subagent it follows in the session's state file, and read whatever file that path named. A changed state file could so make it send some other file to Emet. The path must now lie in the session's own `subagents` folder and must not be a symbolic link, and neither must that folder. The file is opened without following a link and judged once it is open, and a file that has a second name (a hard link) is refused. Otherwise that subagent is no longer followed, and the log says `subagent skipped`, without the path.
- A symbolic link in the retry queue is removed and the file it points to is never read or sent; the log says `queued batch dropped, it is a link`. The plugin's own files (state, status, pause, queue) are no longer read through a link on macOS and Linux, and a link in place of the `state` or `queue` folder is removed and the folder made again.
- A damaged subagent entry in the session's state file no longer stops the run. It is passed over and the rest of the session is sent.
- Very deeply nested JSON no longer reaches Python's parser. On Python 3.9, which is the `python3` of macOS, parsing nesting about a thousand levels deep can end the background process with `Fatal Python error: Cannot recover from stack overflow`, which no error handling can catch, and macOS then shows a "Python quit unexpectedly" window. A server answer, a line of the session file, a subagent file or one of the plugin's own files nested deeper than 200 levels is now treated as unreadable, like any other damaged input. The session itself was never affected.

## [0.1.4] - 2026-11-19

### Added

- The plugin has an icon, `claude/.claude-plugin/icon.png`, the Emet logomark, for its listing in Anthropic's plugin directory.
- `docs/contact.md` explains how to buy Emet before installing, and both READMEs link it ahead of the install steps.

### Changed

- The key check reads, it no longer sends. At session start the plugin posted an empty turn and took Emet's `400` as "key accepted", which used one of the Application's sends each time. It now makes one `GET` on the report endpoint for an id no trace has. `403` or `401` is reported as `rejected`. A `404` is reported as `accepted` only when it is Emet's own answer; any other `404`, such as a page from a proxy, shows as `unexpected 404`. The check waits 5 seconds at most, follows no redirect, and is skipped after `/clear` and after compaction, where the key cannot have changed.
- When Emet cannot be reached, the plugin pauses. Before, every hook of every session waited up to 15 seconds for an answer, one after another. After a failed send the plugin now makes no request for about 30 seconds, twice as long after each further failure, 10 minutes at most, or for the time Emet names in `Retry-After` (an hour at most). Every session on the machine shares the pause. Turns are still read and queued, and are sent once Emet answers again. `/emet:status` shows the pause as `paused_until`.
- Times in the session file are read as instants, not compared as text. Today's sessions are sent exactly as before. If Claude Code ever writes a different form (another number of fraction digits, or a numeric offset in place of `Z`), spans keep the right order and length, and every time still reaches Emet in one form: UTC with milliseconds and `Z`. A time with no time zone is treated as unreadable, and an entry with no readable time takes the time of the entry before it. A turn starts at its earliest time and a subagent ends at its latest, so no span starts before the turn it belongs to.

### Fixed

- Nothing more is sent to a turn Emet refused. Before, what was added later to such a turn (a late tool result, a background subagent) was sent to a trace that does not exist, and the batches after a refused one were still sent. The rest of a refused turn is now dropped and logged, and the session goes on with the next turn. This also holds when the turn waited in the queue and another session was the one that sent it and got the refusal.
- An answer that is not readable HTTP, or an error whose body is cut off, is handled like any other failed send: the turn is queued and the pause starts. Before, it stopped the run with `worker error` in the log, the session's progress was not saved, and turns already delivered in that run were sent again by the next one.
- A send that is answered with a redirect is not followed. Before, the plugin followed a `301`, `302` or `303` and sent the key on to the address it named, and an answer from that address could count the turn as delivered though Emet never received it. A redirect is now handled like an Emet that cannot be reached: the turn is queued, the pause starts, and nothing is dropped. The log and `/emet:status` show it as `302 redirected`, without the address.
- A background worker that cannot be started no longer surfaces an error in the hook. The failure is written to the log as `worker start failed`, and the hook always exits 0 with no output.
- A turn that cannot be read no longer stops the session from being sent. Before, a turn with no usable time raised an error on every run, the session's progress was never saved, and nothing after that turn was sent. Such a turn is now skipped, written to the log as `turn skipped`, and counted by `/emet:status`; everything after it is sent. The same holds for any other error while building a turn.
- A line of the session file nested too deep to read is passed over like any other unreadable line. Before, it raised an error on every run and stopped the session from being sent. A saved tool output or a subagent file that cannot be opened no longer costs the whole turn: the turn is sent with the result the session file holds, or without that subagent.
- A time that Emet would refuse, such as an empty one, is no longer sent. Emet answered `400` and the whole batch was dropped.
- A turn whose first entry has no id is sent under an id made from its place in the session file. Before, every such turn of a session was sent under the id `None` and would have merged into one trace.

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

[Unreleased]: https://github.com/FUSEis/emet-plugin/compare/v0.1.4...HEAD
[0.1.4]: https://github.com/FUSEis/emet-plugin/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/FUSEis/emet-plugin/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/FUSEis/emet-plugin/compare/v0.1.0...v0.1.2
[0.1.0]: https://github.com/FUSEis/emet-plugin/releases/tag/v0.1.0
