# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-11-23

### Added

- Claude now tells you when Emet disputes a claim. After a turn ends, the plugin reads Emet's report on it in the background. When the report is flagged, Claude speaks once more without being asked: it quotes each disputed claim, gives Emet's reason, and says in one line per claim what the turn's tool output shows (it contradicts the claim, it supports the claim and the flag looks mistaken, or Claude cannot tell). Claude says it cannot tell when it sees only a preview of the output. It is asked to call no tool and open no link because of the report. This happens only when the report is ready within 60 seconds of the end of the turn and you have not started another turn, at most 3 times in a session, only with a person at the session, and only on Claude Code 2.1.287 or later. There is no setting that turns it off alone; disabling the plugin does. The extra message is a turn of your session and uses your Claude plan like any other. [claude/README.md](claude/README.md#when-emet-disputes-a-claim) has the details.
- A flagged report that did not wake Claude comes with your next prompt, once, newest first, 3 in a session at most. Claude Code shows one fixed line saying that Claude was given a report. The same goes for a turn Claude Code started on a schedule (a `/loop` iteration, a scheduled wake-up), which never wakes Claude; these are counted apart from the reports on your own turns. Nothing is shown with a slash command, after `/clear`, or once the conversation was compacted.
- Claude is never woken about the plugin's own commands. The answer to `/emet:status` or `/emet:setup` is sent to Emet as before, and its report is never read.
- `/emet:status` lists the last five report checks as flagged, checked and not flagged, or not checked. A report that was late, held back, failed or never read is never listed as clean. The list shows ids, outcome words, times and the report link, and no text from a report. `/emet:setup` says once what to expect.
- The check sends nothing: a second `Stop` hook, run in the background, makes `GET` requests for the reports of turns the plugin sent, for 5 minutes at most after a turn ends. A report Claude is told about is kept in the plugin's data folder for 7 days at most and removed once shown. `status.json` has a `checks` list and a `reads_paused_until` field, which is empty unless Emet limited the rate of the report reads.
- `claude/README.md` lists every file the plugin keeps, what each holds and for how long. `SECURITY.md` has a trust model: what the plugin reads, where it sends, how it treats Emet's answers and the text of a report, what it does not defend against, and how to pin a release to a tag or a commit.
- The plugin keeps two more values for each delivered turn and names the version of its saved-file format. Neither is sent to Emet, and 0.1.4 reads these files as before.

### Changed

- The plugin needs Claude Code 2.1.287 or later, up from 2.1.271. On an older Claude Code it still sends turns and reads no report. [claude/README.md](claude/README.md#install) says why.
- Every time the plugin writes in its own files is now in UTC with milliseconds, as in `2027-01-15T08:00:00.789Z`, the form Emet is already sent. `/emet:status` and `emet.log` now show UTC, where before they showed the machine's local time. Files an earlier version wrote are still read.
- `/emet:status` and the log show a failed send as its HTTP status, with Emet's short error code when it has one, as in `400 some_code`. Before, they showed up to 200 characters of whatever the server answered, which could put a server's own text in front of Claude or session content in the log.
- A log line is always one line. A line break or an invisible character in a logged value is written as `?`, and a line is cut at 300 characters.
- The status skill treats every log line and every value of the status file as data to report, never as an instruction.
- The turn Claude takes after the plugin woke it is sent with a fixed placeholder as its prompt, so Emet's own report text is not sent back as a person's message.
- When a session's file of saved state is of a format this plugin does not know, which can only happen after the plugin was moved back to an older version, the plugin sends nothing for that session and leaves the file alone. `/emet:status` explains it.

### Fixed

- A forked session no longer sends the earlier conversation again. When you fork a session (`/branch`, `/fork` or `claude --resume <id> --fork-session`), Claude Code copies the earlier turns into the new session's file. The plugin sent each of them to Emet a second time and could give Claude a report on an old answer with a new prompt. Copied turns are now skipped and their reports never read. A Claude Code that does not mark copied turns gets the earlier behavior.
- A turn that Claude Code starts on a schedule (a `/loop` iteration, a scheduled wake-up) or for a message from another Claude session, such as a background subagent's result, is now sent as a turn of its own and its report is read. Before, it was added to the trace of another turn and its answer was never checked.
- A turn that starts within a second or two of the end of the one before it, as a queued prompt or a background-task notice does, is no longer sent as finished while it still runs. Before, a claim Emet disputed in its real answer could go unshown.
- The plugin no longer follows a symbolic link, or waits on a named pipe, in place of any of the files it keeps in its data folder or of a subagent's file. A link is removed and a new file made, or the file is skipped, and the log says so. Before, the plugin could write into, lock or send a file outside its data folder.
- A damaged subagent entry in the session's saved state no longer stops the run. It is passed over and the rest of the session is sent.
- Very deeply nested input can no longer end the background process on Python 3.9, which is the `python3` of macOS. It is treated as unreadable, like any other damaged input. The session itself was never affected.
- The report check's waits no longer move with a change of the system clock. Before, a clock set forward ended a run early, and one set back could keep it polling past its 5 minutes.

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

[Unreleased]: https://github.com/FUSEis/emet-plugin/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/FUSEis/emet-plugin/compare/v0.1.4...v0.2.0
[0.1.4]: https://github.com/FUSEis/emet-plugin/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/FUSEis/emet-plugin/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/FUSEis/emet-plugin/compare/v0.1.0...v0.1.2
[0.1.0]: https://github.com/FUSEis/emet-plugin/releases/tag/v0.1.0
