# Emet for Claude Code

Sends each Claude Code turn to [Emet](https://emet.so), which checks the claims in the answer against the tool results behind them. Every finished turn shows up in Emet as a conversation you can review, with each claim marked verified, contradicted or unverifiable, and with the model, token and tool usage of the session.

## Install

The plugin needs an Emet account for your organization. If you don't have one yet, [contact us](https://github.com/FUSEis/emet-plugin/blob/main/docs/contact.md) first.

In Claude Code:

```text
/plugin marketplace add FUSEis/emet-plugin
/plugin install emet@emet-plugin
/plugin configure emet@emet-plugin
```

Then restart Claude Code and run `/emet:setup`. To install it with your agent instead, or for a whole organization, see the [repository README](https://github.com/FUSEis/emet-plugin#install-with-your-agent).

Requirements: Claude Code 2.1.271 or later, and `python3` 3.9 or later on the `PATH` (macOS and most Linux distributions include it). The plugin installs no packages.

## Options

| Option | Required | Default | What it does |
| --- | --- | --- | --- |
| `emet_key` | yes | | Your team's Emet key, from **Applications**, then **Manage keys**. Kept in Claude Code's secure credential store (the Keychain on macOS), never in `settings.json`. |
| `tenant_ref` | yes | | Team ID: the External ID of your team, from **Teams** in Emet. |
| `emet_email` | no | your Claude sign-in | Your Emet email, so Emet attributes your turns to you. Leave it blank to use the email you sign in to Claude with; set it when you use an API key, Bedrock, Vertex AI or a gateway, or when the two emails differ. |
| `capture_mode` | no | `sanitized` | How much content is sent; see below. |

In an organization, an administrator usually sets the key, the Team ID and the capture mode for everyone. You only set your email, if you do not sign in to Claude with the email of your Emet account: see [docs/enterprise.md](https://github.com/FUSEis/emet-plugin/blob/main/docs/enterprise.md).

## Use it

- **Every turn, automatically.** Work as usual. When a turn ends, the plugin sends it in the background and the session never waits. Open Emet's **Conversations** page to see each answer's claims and verdicts.
- **`/emet:setup`**: connects the plugin to your Emet key and team, and confirms Emet accepts the key.
- **`/emet:status`**: links the Emet report of your latest turns, and shows whether turns are reaching Emet, who they are sent as, what is queued for retry, and what any error means.

## What is sent

For each finished turn, one trace:

| Sent | `sanitized` | `full` | `metadata` |
| --- | --- | --- | --- |
| Model name, token counts (input, output, cache, thinking), stop reason, timings | yes | yes | yes |
| Tool names, call ids, success or error | yes | yes | yes |
| Repository folder name, git branch, Claude Code version | yes | yes | yes |
| Your email (see `emet_email`), so Emet can attribute the turn to you | yes | yes | yes |
| Your prompt and Claude's answers | redacted | yes | no |
| Tool arguments and the tool results Claude received | redacted | yes | no |
| Subagent prompts, calls and results | redacted | yes | no |

- **Redaction** (`sanitized`) replaces API keys and tokens (Anthropic, OpenAI, AWS, GitHub, GitLab, Slack, Stripe, Google, npm, Emet), JWTs, private keys, bearer tokens, passwords in URLs and `SECRET=`-style assignments with `[REDACTED:kind]`. It is pattern-based: it cannot catch a secret in a format it does not know, so choose `metadata` where that matters. Emet keys are replaced in every mode, `full` included.
- **Images and other binary content** are never sent in any mode. A placeholder such as `[image content not shown: image/png, 412 KB]` takes their place, and the span is marked truncated so Emet does not treat it as complete.
- **What Claude saw, not more.** Tool results are sent as Claude received them, except that output Claude Code saved to disk because it was too large is sent in full.
- **Nothing else.** Thinking blocks, system prompts, tool definitions and files Claude did not read are not sent.
- `metadata` keeps usage and timing reports working, but Emet cannot check claims without content.

## Where data goes and what stays local

The plugin sends only to Emet (`https://app.emet.so`), over HTTPS. It refuses any other destination, whatever the environment says. The one exception is an Emet on the same machine, for plugin development.

It keeps a private folder in Claude Code's plugin data directory (`~/.claude/plugins/data/…`). On macOS and Linux the plugin sets the folder so that only your user can open it. On Windows it has the access rules of the folder it sits in.

One kind of file in it can hold text from your session:

| File | What it holds | Kept until |
| --- | --- | --- |
| `queue/*.json` | Turns that could not be delivered yet, whole, as your capture mode sends them: prompts, answers and tool results in `sanitized` and `full`, none of them in `metadata`. | Emet accepts or refuses the turn. It is retried on the next turn, oldest first. At most 500 files or 256 MB; the oldest are dropped beyond that. There is no time limit. |

The other files hold ids, times, counts and HTTP statuses. They hold no text from a turn:

| File | What it holds | Kept until |
| --- | --- | --- |
| `state/<session>.json` | How far the session's transcript has been read: a position in the file, the id of the last turn sent, and for each subagent still followed the path of its transcript, its ids and a position. | The plugin is uninstalled. |
| `state/<session>.pending.jsonl` | One line for each turn Emet accepted: Emet's ids of the trace and of its conversation, the time, and the address that accepted it. Also a line for a turn Emet refused (its id and the HTTP status), for a turn that could not be read (the name of the error's type), and for the end of each run (the time and the number of turns queued). | The plugin is uninstalled. |
| `pause.json` | The time before which the plugin asks Emet nothing, and the length of the last wait, written after Emet could not be reached. | Emet answers again after the pause has run out. |
| `status.json` | What `/emet:status` reports: the plugin version, Team ID, capture mode, the email sent, the public start of the key, the result of the key check, counts, the pause time, and for the last five sessions the repository folder name, the time and the report link. | Rewritten at every run. |
| `emet.log`, `emet.log.1` | One line for each thing the plugin did: the time, ids, counts, HTTP statuses, Emet's short error codes and the names of error types. | The log starts again at 1 MB, and one earlier file is kept. |
| `*.lock` | Nothing. They stop two processes from writing one file at once. | The plugin is uninstalled. |

The log and `status.json` never hold the key, a prompt, an answer or a tool result. Of an error Emet answers with, they keep the HTTP status and a short code of lowercase letters and underscores, never the message.

Emet's handling of the data it receives is covered by the [Emet privacy policy](https://emet.so/privacy).

## How it works

The plugin registers four hooks (`SessionStart`, `Stop`, `SubagentStop`, `SessionEnd`). Each one returns at once and starts a short-lived background process, which:

1. reads the session transcript from where it last stopped;
2. groups it into turns (a prompt and everything up to the next prompt), and each turn into model calls, tool calls and subagents;
3. applies the capture mode, then sends the turn to `POST /api/v1/traces/` with the key as a bearer token;
4. queues the turn if Emet cannot be reached, and retries it on a later run. After a failed send every session on the machine waits before asking Emet again: about 30 seconds at first, twice as long after each further failure, 10 minutes at most, or the time Emet names in `Retry-After`. Turns are still read and queued meanwhile.

A turn still waiting for its answer is read again next time, and a background subagent that finishes later is added to the turn that launched it. When a session starts, is resumed or is forked, `SessionStart` also checks that Emet accepts the key, with one `GET /api/v1/traces/{id}/report/` for an id no trace has. It sends no trace. Unless `emet_email` is set, each run reads the signed-in email from Claude Code's account file (`~/.claude.json`), keeping only `oauthAccount.emailAddress`.

## Turn it off

- For one project: `claude plugin disable emet@emet-plugin --scope project`.
- Everywhere: `claude plugin uninstall emet@emet-plugin`. This also deletes the plugin's data folder, including any turns still queued.

## Troubleshooting

Run `/emet:status`. It reads `status.json` and the log and explains the most recent problem. The usual causes are a missing or revoked key (`401` or `403`), or a network or proxy blocking `app.emet.so`.

## License

[Apache License 2.0](LICENSE).
