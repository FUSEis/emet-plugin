# Emet for Claude Code

Sends each Claude Code turn to [Emet](https://emet.so), which checks the claims in the answer against the tool results behind them. Every finished turn shows up in Emet as a conversation you can review, with each claim marked verified, contradicted or unverifiable, and with the model, token and tool usage of the session.

## Install

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
| `api_key` | yes | | Your Emet application key, from its **Keys** page. Kept in the macOS Keychain, or in Claude Code's credentials file (`~/.claude/.credentials.json`, readable only by you) where there is no keychain. |
| `tenant_ref` | yes | | Team ID: the External ID of your team on Emet's **Teams** page. |
| `capture_mode` | no | `sanitized` | How much content is sent; see below. |

## Use it

- **Every turn, automatically.** Work as usual. When a turn ends, the plugin sends it in the background and the session never waits. Open Emet's **Conversations** page to see each answer's claims and verdicts.
- **`/emet:setup`**: connects the plugin to your Emet key and team, and confirms Emet accepts the key.
- **`/emet:status`**: shows whether turns are reaching Emet, when the last one was delivered, what is queued for retry, and what any error means.

## What is sent

For each finished turn, one trace:

| Sent | `sanitized` | `full` | `metadata` |
| --- | --- | --- | --- |
| Model name, token counts (input, output, cache, thinking), stop reason, timings | yes | yes | yes |
| Tool names, call ids, success or error | yes | yes | yes |
| Repository folder name, git branch, Claude Code version | yes | yes | yes |
| Your prompt and Claude's answers | redacted | yes | no |
| Tool arguments and the tool results Claude received | redacted | yes | no |
| Subagent prompts, calls and results | redacted | yes | no |

- **Redaction** (`sanitized`) replaces API keys and tokens (Anthropic, OpenAI, AWS, GitHub, GitLab, Slack, Stripe, Google, npm, Emet), JWTs, private keys, bearer tokens, passwords in URLs and `SECRET=`-style assignments with `[REDACTED:kind]`. It is pattern-based: it cannot catch a secret in a format it does not know, so choose `metadata` where that matters.
- **Images and other binary content** are never sent in any mode. A placeholder such as `[image content not shown: image/png, 412 KB]` takes their place, and the span is marked truncated so Emet does not treat it as complete.
- **What Claude saw, not more.** Tool results are sent as Claude received them, except that output Claude Code saved to disk because it was too large is sent in full.
- **Nothing else.** Thinking blocks, system prompts, tool definitions and files Claude did not read are not sent.
- `metadata` keeps usage and timing reports working, but Emet cannot check claims without content.

## Where data goes and what stays local

The plugin sends only to Emet (`https://app.emet.so`), over HTTPS. It keeps a private folder in Claude Code's plugin data directory (`~/.claude/plugins/data/…`), readable only by you:

- `state/`: how far each session's transcript has been read.
- `queue/`: turns that could not be delivered yet, retried oldest first on the next turn. At most 500 turns or 256 MB; the oldest are dropped beyond that.
- `status.json` and `emet.log`: what `/emet:status` reports. They hold ids, counts and HTTP statuses, never content or the key.

Emet's handling of the data it receives is covered by the [Emet privacy policy](https://emet.so/privacy).

## How it works

The plugin registers four hooks (`SessionStart`, `Stop`, `SubagentStop`, `SessionEnd`). Each one returns at once and starts a short-lived background process, which:

1. reads the session transcript from where it last stopped;
2. groups it into turns (a prompt and everything up to the next prompt), and each turn into model calls, tool calls and subagents;
3. applies the capture mode, then sends the turn to `POST /api/v1/traces/` with the key as a bearer token;
4. queues the turn if Emet cannot be reached, and retries it on the next run.

A turn still waiting for its answer is read again next time, and a background subagent that finishes later is added to the turn that launched it. `SessionStart` also checks that Emet accepts the key, without sending a trace.

## Turn it off

- For one project: `claude plugin disable emet@emet-plugin --scope project`.
- Everywhere: `claude plugin uninstall emet@emet-plugin`. This also deletes the plugin's data folder, including any turns still queued.

## Troubleshooting

Run `/emet:status`. It reads `status.json` and the log and explains the most recent problem. The usual causes are a missing or revoked key (`401` or `403`), or a network or proxy blocking `app.emet.so`.

## License

[Apache License 2.0](LICENSE).
