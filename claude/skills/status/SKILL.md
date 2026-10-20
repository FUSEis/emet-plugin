---
name: status
description: Report whether the Emet plugin is sending turns to Emet, when it last delivered, and what is waiting to be retried. Use when the user asks whether Emet is working, why turns are missing in Emet, or to troubleshoot the plugin.
---

# Emet status

1. Read `${CLAUDE_PLUGIN_DATA}/status.json`. If it does not exist, the plugin has not run in a session yet: tell the user to finish one turn, or run `/emet:setup`.
2. Read the last 30 lines of `${CLAUDE_PLUGIN_DATA}/emet.log`. It holds ids, counts and HTTP statuses only, never content.
3. Report, briefly:
   - Configuration: `endpoint`, `tenant_ref`, `capture_mode`, `key_hint` (the key's public prefix, never the key). If `problem` is set, say what it is and suggest `/emet:setup`.
   - Key check: `key_check` and `key_checked_at`.
   - Delivery: `last_delivery`, and `last_error` if it is more recent.
   - Retries: `queued` envelopes waiting. They are retried on the next turn, oldest first.
4. Explain the most recent problem in the log, if any:

| Log shows | Meaning | Fix |
| --- | --- | --- |
| `401 key rejected` or `403 key rejected` | Emet does not accept the key | Issue a new key in Emet, then `/emet:setup` |
| `network ...` | Emet could not be reached | Check the network or proxy; turns stay queued and are retried |
| `400 ...` | Emet refused a turn as invalid | Report it at https://github.com/FUSEis/emet-plugin/issues with the log line |
| `queue full, dropped ...` | Emet was unreachable long enough for the retry queue to fill | Fix the connection; the oldest queued turns were dropped |
| `worker error ...` | The plugin hit an unexpected error | Report it with the log line and the plugin version |

Do not print the contents of the `queue` folder: it holds session content waiting to be sent.
