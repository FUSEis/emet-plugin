# Security

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: on this repository, open **Security**, then **Report a vulnerability**, or go straight to the [report form](https://github.com/FUSEis/emet-plugin/security/advisories/new). Do not open a public issue.

Include the plugin version (`plugin.json`), the Claude Code version, your operating system, and the steps to reproduce. We acknowledge reports within three business days and keep you updated until a fix is released. A release that fixes a vulnerability says so in its notes.

## Trust model

The plugin runs as you, inside Claude Code's hooks. It holds one credential, the Emet key, and it reads things it did not write: your session and Emet's answers. One of those answers, Emet's report on a turn, can reach Claude as text. This section says what the plugin reads, where it sends, what it does not trust, and what it does not defend against.

### What it reads

- **The session transcript** Claude Code names in the hook input, and in the folder Claude Code keeps beside it, that session's subagent transcripts and the tool outputs Claude Code saved to disk. A path the transcript names is used only when it resolves into that folder.
- **Claude Code's account file** (`~/.claude.json`, or the one in `CLAUDE_CONFIG_DIR`), for one value, `oauthAccount.emailAddress`. It is not read when `emet_email` is set.
- **Its options**, from the environment Claude Code gives a hook.
- **Its own data folder**, listed file by file in [claude/README.md](claude/README.md#where-data-goes-and-what-stays-local).
- **The prompt you send**, which Claude Code gives the hook on `UserPromptSubmit`. That hook looks at how the prompt begins, to tell a command from a prompt. It keeps none of it and sends nothing.

It opens no other file. It does not read your project: what it sends of a file is what the transcript already holds. It writes only inside its data folder.

### Where it sends

Only to `https://app.emet.so`, with the key as a bearer token: finished turns, a key check when a session starts, and report reads. The key check is one read of the report endpoint for an id no trace has, so it sends no trace and reads no report. A report read is a `GET` for the report of one turn the plugin sent, by the id Emet returned for it, and sends nothing else. Any other address is refused before a request is made, except an Emet on the same machine (`localhost`, `127.0.0.1`, `::1`), which exists for plugin development. No request follows a redirect, so the key and the turns never go on to an address Emet's answer names.

### What it does not trust

**Emet's answers to a send, a key check or a report that is not ready.** The plugin acts on little of such an answer: the HTTP status, the wait a `Retry-After` header names (an hour at most), two ids, each kept only when it is a UUID, and the short code Emet puts in its own `404` answers. Nothing in an answer is executed, and no text from one of these answers is shown to Claude. Of an error, the log and `status.json`, which `/emet:status` reads, keep the HTTP status and a short code of lowercase letters and underscores, 40 characters at most. The error message is not kept.

**Emet's reports.** A report is written by a model that read tool results and web pages, so any sentence in it can be an attempt to steer Claude. The plugin reads a report after each turn, and tells Claude about one that Emet flagged. When the plugin tells Claude about a report:

- A report over 2 MB, of a version the plugin does not know, or with a status it does not know is not shown at all.
- Each piece of text is cut to a fixed length, put on one line, and stripped of control and invisible characters.
- All of it sits in one data block, as JSON strings. Angle brackets are escaped, so nothing in a report can close the block.
- The words before and after the block are fixed. They tell Claude that the block is data, and not to follow an instruction, call a tool or open a link because of it. They ask Claude to say what the tool output of that turn shows about each claim, to quote the few words of it that it relies on, never a key, token, password or other secret, to read that output as evidence and never as an instruction, to say it cannot tell when it sees only a preview or a summary of the output, and to treat Emet's reason and correction as claims to check. The whole message is 4,000 characters at most.
- Nothing from a report is executed, used as a file name, or used as an address. The link in the message is built from the plugin's own values.
- Of a report, the plugin saves only text rendered this way, in `verdicts/`, for 7 days at most. The report itself is never saved. Claude Code keeps the message it was handed in the session transcript, as it keeps any message.
- The log and `status.json`, which `/emet:status` reads, keep ids, times and one of a fixed list of outcome words for a report. They keep no text from it.
- Claude is woken about at most 3 reports in a session, and only within 60 seconds of the end of the turn. The turn Claude takes to answer is sent to Emet, and its report is never read, so a report cannot start a chain of messages.
- A flagged report Claude was not woken with is given to Claude with your next prompt in that session, once, for at most 3 reports in a session. Claude is never woken with a report on a turn Claude Code started on a schedule, such as a `/loop` iteration. Such a report comes with the next prompt of the session, counted apart, for at most 3 more. The report on the answer to that prompt is read and recorded, and Claude is never told of it.

This lowers the chance that a report steers Claude. It cannot remove it, because Claude reads the block. Someone who can write what Emet checks, such as a web page a tool fetched during the turn, can try to shape the reasons Emet gives, and so the text in the block. Claude also reads the tool output of the turn for its message, web pages included, so a line planted there can try to shape what Claude says the output shows, and which words of it Claude quotes to you. What Claude quotes is part of its message, and that message is sent to Emet like any answer. Redaction there works by pattern, so a secret no pattern knows is sent as Claude quoted it.

**A saved report in its data folder.** The hook on your prompt gives Claude the text of a report on an earlier answer, saved in the data folder, when Claude was not woken with it. A file counts as a saved report only when all of it is as the plugin writes one: the plugin's fixed words, and inside them values that the words tell Claude to treat as data and never to act on. Anything else is left alone and never shown. The hook can add to what Claude reads with a prompt. It cannot change the prompt or hold it back.

**The session's text.** The plugin sends it and never acts on it. Nothing in a prompt, an answer or a tool result is executed or used as an address. The hook input itself, the session id and the transcript path, comes from Claude Code and is trusted.

### What is not a boundary

- **Another process running as you.** It can read the transcript and the data folder, queued turns and saved report text included, and it can change the plugin's files. It can also put a file there in the form of a saved report, and the hook on your prompt then gives Claude the values in it with your next prompt, inside the plugin's fixed words. On macOS and Linux the plugin sets its data folder and files to owner-only, which keeps other users out. On Windows the plugin sets no access rules, so the folder is as private as the folder it sits in, normally your user profile. None of this stops a process that already runs as you.
- **Whoever sets Claude Code's environment.** Options reach the plugin through it. It cannot name another host. It can name a program listening on your own machine, which then receives the key and the turns. [docs/enterprise.md](docs/enterprise.md) says how managed settings close this.
- **Redaction.** `sanitized` removes the secrets its patterns know. Use `metadata` where that is not enough.

## Scope

In scope: anything in this repository that breaks the trust model above. For example a way to make the plugin send the key or a turn anywhere but Emet, send content in `metadata` mode, leak the key into a file or the conversation, read a file other than the ones listed above, write outside its data folder, block or break a session, change or hold back a prompt, let text from Emet reach Claude outside the data block, or make Claude speak more often or later than the limits above allow.

Out of scope: secrets that the `sanitized` redaction patterns do not recognize. Redaction is best effort and documented as such; use `metadata` mode where that matters. Suggestions for new patterns are welcome as ordinary issues, without the secret. Also out of scope is anything that needs a process already running as the user, or control of Claude Code's environment, as described above.

## Supported versions and pinning

Security fixes go into the latest release. An install with no ref follows the `main` branch, which can be ahead of the latest release.

- **Pin a release tag.** Add the marketplace as `FUSEis/emet-plugin#vX.Y.Z`, or set `"ref": "vX.Y.Z"` on the marketplace source in managed settings, as [docs/enterprise.md](docs/enterprise.md) shows. Move the pin forward when a release notes a security fix.
- **Pin a commit for a stricter setup.** A tag can be moved; a commit cannot. Set `ref` on the marketplace source to the full commit SHA of the release you reviewed. `git ls-remote https://github.com/FUSEis/emet-plugin "refs/tags/vX.Y.Z^{}" "refs/tags/vX.Y.Z"` prints it; when two lines come back, the commit is the one ending in `^{}`. This needs Claude Code 2.1.324 or later on every machine. The plugin is a folder of this repository, so pinning the marketplace pins the plugin's code with it.

Claude Code's [marketplace reference](https://code.claude.com/docs/en/plugins/marketplace-reference) describes both forms.
