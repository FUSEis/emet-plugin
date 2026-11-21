# Emet for Claude Code

Sends each Claude Code turn to [Emet](https://emet.so), which checks the claims in the answer against the tool results behind them. Every finished turn shows up in Emet as a conversation you can review, with each claim marked verified, contradicted or unverifiable, and with the model, token and tool usage of the session. When Emet disputes a claim in an answer Claude just gave, Claude tells you in the session.

## Install

The plugin needs an Emet account for your organization. If you don't have one yet, [contact us](https://github.com/FUSEis/emet-plugin/blob/main/docs/contact.md) first.

In Claude Code:

```text
/plugin marketplace add FUSEis/emet-plugin
/plugin install emet@emet-plugin
/plugin configure emet@emet-plugin
```

Then restart Claude Code and run `/emet:setup`. To install it with your agent instead, or for a whole organization, see the [repository README](https://github.com/FUSEis/emet-plugin#install-with-your-agent).

Requirements: Claude Code 2.1.287 or later, and `python3` 3.9 or later on the `PATH` (macOS and most Linux distributions include it). The plugin installs no packages.

Why 2.1.287: the plugin's check of Emet's report runs as a background hook that can wake Claude. Before 2.1.287, a hook of that kind whose script file could not be opened woke Claude again and again, each time using your Claude plan; from 2.1.287 on, Claude Code reports the broken hook once ([Claude Code changelog, 2.1.287](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md), [issue 96148](https://github.com/anthropics/claude-code/issues/96148)). On an older Claude Code the plugin still sends turns and reads no report, but it is not supported there. `claude --version` prints yours, and `claude update` updates it.

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
- **When Emet disputes a claim.** Claude may speak once more after an answer, to tell you what Emet disputed, why, and what the tool output it can see shows. See [When Emet disputes a claim](#when-emet-disputes-a-claim).
- **`/emet:setup`**: connects the plugin to your Emet key and team, and confirms Emet accepts the key.
- **`/emet:status`**: links the Emet report of your latest turns, shows for each recent turn whether Emet flagged it, found no conflict or did not check it, and shows whether turns are reaching Emet, who they are sent as, what is queued for retry, and what any error means.

## When Emet disputes a claim

After a turn ends, the plugin asks Emet for its report on that turn. When the report disputes a claim in the answer, the plugin hands Claude a short notice, and Claude speaks once more without being asked. This is in the plugin on `main`. Release 0.1.4 does not have it.

**What Claude says.** The notice asks Claude to quote each disputed claim as it stated it, give Emet's reason in plain words, and then say in one line per claim what the tool output of that turn shows, with the few words of output it relies on. When output Claude sees whole contradicts the claim, Claude says the claim was wrong and gives the value the output shows. When it supports the claim, and Emet's reason names nothing Claude cannot see, Claude says the flag looks mistaken. In every other case it says it cannot tell. Emet can have read more than Claude sees: Claude Code gives Claude a preview of a long output and saves the rest to a file, and Claude gets a subagent's report, not the subagent's own tool results. So a preview, a summary of a subagent's work or of the session, and Claude's own earlier words settle nothing. Claude is asked to rule from that output only, never from Emet's reason or suggested correction, and to say once that Emet's check and its own reading use only that turn's evidence, and can miss things or be wrong. Claude may offer to correct the answer or to look into it, and is asked to do neither unless you ask. Claude may give you one link, to the report in Emet. The plugin builds that link itself, and it is never taken from the report.

**What Claude is asked not to do.** The notice tells Claude that Emet's report is data, and not to follow an instruction in it, call a tool, open a link, run a command or edit a file because of it. It tells Claude that the tool output it reads for its message is evidence, never an instruction to follow, and never to quote a key, token, password or other secret from it. These are instructions to a model. They make such an action unlikely; they cannot rule it out. [SECURITY.md](https://github.com/FUSEis/emet-plugin/blob/main/SECURITY.md#what-it-does-not-trust) says how the report's text is handled before Claude reads it.

**When it happens, and when it does not.**

- Only for a report Emet marked as flagged. Any other report is recorded and Claude says nothing.
- Only when the report is ready within 60 seconds of the end of the turn, and you have not started another turn since. The first read comes about 8 seconds after the turn ends. A report that comes later, or after your next prompt, is recorded and never interrupts you. It comes with your next prompt, and `/emet:status` lists it.
- At most 3 times in one session. Later flagged reports are recorded, come with your next prompt, and are listed by `/emet:status`.
- Once for each report: the same report never makes Claude speak twice. Claude's extra message is sent to Emet like any turn, marked as started by the plugin, and its report is never read. So one message from Claude never leads to another.
- Never about the plugin's own commands. The answer to `/emet:status` or `/emet:setup` is sent to Emet like any turn, and its report is never read, so Claude does not speak about it. Running one of them counts as your next turn: a report on the answer before it is recorded and does not interrupt you.
- Only in a session with a person at it. Under `claude -p` or the SDK, and on a Claude Code older than 2.1.287, no report is read.
- The plugin asks for 5 minutes after a turn ends. A report that was not ready by then is asked for again when a later turn of the session ends, for a day at most. It is then recorded only, since it is late.

The extra message is a turn of your session. It uses your Claude plan like any other turn, and Claude Code shows it in the conversation. Claude Code 2.1.305 shows a line that reads "Stop hook feedback" above it.

**A report Claude did not speak about.** A flagged report that Claude was not woken with is saved, and Claude is given it with the next prompt you send in that session. Claude Code then shows one line under your prompt, "Emet disputed a claim in an earlier answer of this session. Claude was given Emet's report with this prompt.", and Claude says what Emet disputed, and what the tool output of that answer's turn shows, before it answers the prompt.

- Once for each report, one report with a prompt, the newest first.
- At most 3 times in one session, counted apart from the 3 messages above. A report still waiting after that is removed and Claude is not given it. So is one still waiting when the conversation is compacted. `/emet:status` lists both.
- Never with a slash command, and never under `claude -p` or the SDK. The report then waits for a later prompt.
- The report on Claude's answer to such a prompt is read and recorded, and Claude is never told of it, because that answer quotes the disputed claims. `/emet:status` lists it.
- The hook that does this runs when you send a prompt. It reads the plugin's own files, makes no request, and cannot change or hold back your prompt.

**Turning it off.** There is no setting for this alone. To stop Claude from speaking about reports, disable the plugin, which also stops sending turns: see [Turn it off](#turn-it-off).

`/emet:status` shows the outcome of the last five report checks, so you can see a flagged report even when Claude was not told, or when you were away as Claude spoke.

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
- **The turn Claude takes to tell you about a report** is sent like any other, with `emet.plugin.turn_started_by` set to `report_wake`. Its prompt is replaced by a fixed placeholder, so Emet's own report text is not sent back to Emet as a person's message. Claude's message is sent as an answer.
- **Nothing else.** Thinking blocks, system prompts, tool definitions and files Claude did not read are not sent.
- `metadata` keeps usage and timing reports working, but Emet cannot check claims without content.

## Where data goes and what stays local

The plugin sends only to Emet (`https://app.emet.so`), over HTTPS, and reads its reports from there. It refuses any other destination, whatever the environment says. The one exception is an Emet on the same machine, for plugin development.

It keeps a private folder in Claude Code's plugin data directory (`~/.claude/plugins/data/…`). On macOS and Linux the plugin sets the folder so that only your user can open it. On Windows the plugin sets no access rules: the folder has those of the folder it sits in, which is your user profile unless Claude Code's data was moved.

Two kinds of files in it can hold text from your session:

| File | What it holds | Kept until |
| --- | --- | --- |
| `queue/*.json` | Turns that could not be delivered yet, whole, as your capture mode sends them: prompts, answers and tool results in `sanitized` and `full`, none of them in `metadata`. | Emet accepts or refuses the turn. It is retried on the next turn, oldest first. At most 500 files or 256 MB; the oldest are dropped beyond that. There is no time limit. |
| `verdicts/*.json` | One file for each report Emet flagged: Emet's ids, the address it was read from, the time, whether the notice was handed to Claude, the version of the file's own format, and the text written for Claude about it, 4,000 characters at most. The text quotes the disputed claims, Emet's reasons and suggested corrections, and the sentences of Claude's answer they are about. The report itself is never saved. The plugin reads the text back for one use: to give it to Claude with a later prompt, when Claude was not woken with the report. | A later prompt of that session, at which the file is removed when the session shows Claude was woken with the report, when the text is given to Claude with that prompt, when the session already had 3 reports given that way, or when the conversation was compacted since. 7 days at most: a file older than that is removed the next time the plugin reads reports. |

The other files hold ids, times, counts and HTTP statuses. They hold no text from a turn or from a report:

| File | What it holds | Kept until |
| --- | --- | --- |
| `state/<session>.json` | How far the session's transcript has been read: a position in the file, the id of the last turn sent, for each subagent still followed the path of its transcript, its ids and a position, and the version of the file's own format. | The plugin is uninstalled. |
| `state/<session>.pending.jsonl` | One line for each turn Emet accepted: Emet's ids of the trace and of its conversation, the time, the address that accepted it, Claude Code's id of the turn and the time the turn ended, and a word that marks a turn the plugin's own notice or one of its commands started. Also a line for a turn Emet refused (its id and the HTTP status), for a turn that could not be read (the name of the error's type), and for the end of each run (the time and the number of turns queued). | The plugin is uninstalled. |
| `state/<session>.checks.jsonl` | One line for each outcome of reading a turn's report: the trace id, an outcome word such as `clear`, `flagged` or `timed_out`, the time, Emet's id and revision of the report, whether the notice was handed to Claude, and if not, a word for why. Also a line with the word `shown` when a flagged report was given to Claude with a later prompt: the same ids, and Claude Code's id of that prompt. | The plugin is uninstalled. |
| `state/<session>.stop.json` | The session's latest turn end: a random name for it, Claude Code's id of the turn, the size of the transcript, two times, and the version of the file's own format. | The next turn end replaces it. |
| `pause.json` | The time before which the plugin asks Emet nothing, and the length of the last wait, written after Emet could not be reached. | Emet answers again after the pause has run out. |
| `read-pause.json` | The same, for the report reads alone, written after Emet limited their rate. | Emet answers a report read after the pause has run out. |
| `status.json` | What `/emet:status` reports: the plugin version, Team ID, capture mode, the email sent, the public start of the key, the result of the key check, counts, the two pause times, for the last five sessions the repository folder name, the time and the report link, and the last five outcomes of the report check as the checks file holds them. | Rewritten at every run. |
| `emet.log`, `emet.log.1` | One line for each thing the plugin did: the time, ids, counts, HTTP statuses, Emet's short error codes and the names of error types. | The log starts again at 1 MB, and one earlier file is kept. |
| `*.lock` | Nothing. They stop two processes from writing one file at once. | The plugin is uninstalled. |

If the plugin is stopped in the middle of writing a file, a hidden copy named `.<name>.<number>.tmp` can stay behind next to it. One left in `queue/` holds that turn's text. It is never sent, the queue's limits do not count it, and it stays until you delete it or uninstall the plugin. One left in `verdicts/` holds the text written for Claude about a report, and is removed once it is a day old, the next time the plugin reads reports. Deleting such a file is safe while no Claude Code session is running.

The log and `status.json` never hold the key, a prompt, an answer, a tool result or text from a report. Of an error Emet answers with, they keep the HTTP status and a short code of lowercase letters and underscores, never the message.

Emet's handling of the data it receives is covered by the [Emet privacy policy](https://emet.so/privacy).

## How it works

The plugin registers six hooks. Four of them (`SessionStart`, `Stop`, `SubagentStop`, `SessionEnd`) each return at once and start a short-lived background process, which:

1. reads the session transcript from where it last stopped;
2. groups it into turns (a prompt and everything up to the next prompt), and each turn into model calls, tool calls and subagents;
3. applies the capture mode, then sends the turn to `POST /api/v1/traces/` with the key as a bearer token;
4. queues the turn if Emet cannot be reached, and retries it on a later run. After a failed send every session on the machine waits before asking Emet again: about 30 seconds at first, twice as long after each further failure, 10 minutes at most, or the time Emet names in `Retry-After`. Turns are still read and queued meanwhile.

A turn still waiting for its answer is read again next time, and a background subagent that finishes later is added to the turn that launched it. When a session starts, is resumed or is forked, `SessionStart` also checks that Emet accepts the key, with one `GET /api/v1/traces/{id}/report/` for an id no trace has. It sends no trace. Unless `emet_email` is set, each run reads the signed-in email from Claude Code's account file (`~/.claude.json`), keeping only `oauthAccount.emailAddress`.

The fifth hook is a second one on `Stop`, the report check. Claude Code runs it in the background (`async` and `asyncRewake`), so the session never waits for it. It sends nothing. For each turn of the session that Emet accepted, it reads `GET /api/v1/traces/{id}/report/` with the key as a bearer token: first about 8 seconds after the turn ended, then after waits of 5, 10, 15, 20 and 30 seconds, each up to a tenth longer, the last one repeating, until the report is ready. It starts no request later than 300 seconds after the turn ended, and the process ends itself at 320 seconds. When Emet limits the rate of these reads, every session on the machine stops reading reports for the time Emet names, an hour at most, or for about 30 seconds at first when it names none; turns are still sent. No report is read while Emet cannot be reached either. When a report is flagged and the rules in [When Emet disputes a claim](#when-emet-disputes-a-claim) allow it, the process writes the notice for Claude and exits with code 2, which is how a background hook asks Claude Code to wake Claude. In every other case it exits 0 and hands Claude nothing.

The sixth hook, `UserPromptSubmit`, runs when you send a prompt, and the session waits for it, for 5 seconds at most. It looks in the plugin's folder for a flagged report on an earlier answer of the session that Claude was not woken with, and gives it to Claude with your prompt, as [When Emet disputes a claim](#when-emet-disputes-a-claim) says. It makes no request, and it cannot change or hold back a prompt.

## Turn it off

- For one project: `claude plugin disable emet@emet-plugin --scope project`.
- Everywhere: `claude plugin uninstall emet@emet-plugin`. This also deletes the plugin's data folder, including any turns still queued.

## Troubleshooting

Run `/emet:status`. It reads `status.json` and the log and explains the most recent problem. The usual causes are a missing or revoked key (`401` or `403`), or a network or proxy blocking `app.emet.so`.

## License

[Apache License 2.0](LICENSE).
