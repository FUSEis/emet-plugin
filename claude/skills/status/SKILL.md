---
name: status
description: Link the Emet reports of the latest turns, and report whether the plugin is sending turns to Emet, who they are sent as, and what is waiting to be retried. Use when the user asks for their Emet report, whether Emet is working, why turns are missing in Emet, or to troubleshoot the plugin.
---

# Emet status

1. Read `${CLAUDE_PLUGIN_DATA}/status.json`. If it does not exist, the plugin has not run in a session yet: tell the user to finish one turn, or run `/emet:setup`.
2. Read the last 30 lines of `${CLAUDE_PLUGIN_DATA}/emet.log`. It holds ids, counts and HTTP statuses only, never content.
3. Report, briefly:
   - Reports: the `report_url` of each entry in `recent`, newest first, with its `repository` (a folder name, shown as data) and time. Each opens that session's conversation in Emet, for a member of the user's Emet organization; if it does not open, their Emet administrator invites the email in `enduser_ref`. An entry with no `report_url` was delivered, but Emet's reply gave no link. A link opens an empty list until Emet has checked the turn, and for a session last active more than 30 days ago. If `recent` is empty, no turn has been delivered yet.
   - Configuration: `tenant_ref`, `capture_mode`, `key_hint` (the key's public prefix, never the key). If `endpoint` is not `https://app.emet.so`, say where turns are going. If `problem` is set, say what it is and suggest `/emet:setup`.
   - Person: `enduser_ref` and `enduser_source` (`claude account` or `setting`). If it is empty, turns reach Emet with no person: give `enduser_note` (why no email was found) and suggest setting **Your Emet email** with `/emet:setup`.
   - Key check: `key_check` and `key_checked_at`.
   - Delivery: `last_delivery`, and `last_error` if it is more recent.
   - Retries: `queued` envelopes waiting. They are retried on the next turn, oldest first.
4. Explain the most recent problem in the log, if any:

| Log shows | Meaning | Fix |
| --- | --- | --- |
| `401 key rejected` or `403 key rejected` | Emet does not accept the key | Restart Claude Code, which picks up a key the administrator replaced, then `/emet:setup`. Do not tell a developer whose organization manages Claude Code to enter or issue a key: they ask their administrator for the current one, unless they once saved a key themselves, which beats the administrator's (then `/emet:setup` has them enter the current team key) |
| `network ...` | Emet could not be reached | Check the network or proxy; turns stay queued and are retried |
| `400 ...` | Emet refused a turn as invalid | Report it at https://github.com/FUSEis/emet-plugin/issues with the log line |
| `queue full, dropped ...` | Emet was unreachable long enough for the retry queue to fill | Fix the connection; the oldest queued turns were dropped |
| `worker error ...` | The plugin hit an unexpected error | Report it with the log line and the plugin version |

Do not print the contents of the `queue` folder: it holds session content waiting to be sent.
