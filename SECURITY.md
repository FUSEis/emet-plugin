# Security

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: on this repository, open **Security**, then **Report a vulnerability**, or go straight to the [report form](https://github.com/FUSEis/emet-plugin/security/advisories/new). Do not open a public issue.

Include the plugin version (`plugin.json`), the Claude Code version, your operating system, and the steps to reproduce. We acknowledge reports within three business days and keep you updated until a fix is released. A release that fixes a vulnerability says so in its notes.

## Trust model

The plugin runs as you, inside Claude Code's hooks. It holds one credential, the Emet key, and it reads things it did not write: your session and Emet's answers. This section says what it reads, where it sends, what it does not trust, and what it does not defend against.

### What it reads

- **The session transcript** Claude Code names in the hook input, and in the folder Claude Code keeps beside it, that session's subagent transcripts and the tool outputs Claude Code saved to disk. A path the transcript names is used only when it resolves into that folder.
- **Claude Code's account file** (`~/.claude.json`, or the one in `CLAUDE_CONFIG_DIR`), for one value, `oauthAccount.emailAddress`. It is not read when `emet_email` is set.
- **Its options**, from the environment Claude Code gives a hook.
- **Its own data folder**, listed file by file in [claude/README.md](claude/README.md#where-data-goes-and-what-stays-local).

It opens no other file. It does not read your project: what it sends of a file is what the transcript already holds. It writes only inside its data folder.

### Where it sends

Only to `https://app.emet.so`, with the key as a bearer token: finished turns, and a key check when a session starts. The key check is one read of the report endpoint for an id no trace has, so it sends no trace and reads no report. Any other address is refused before a request is made, except an Emet on the same machine (`localhost`, `127.0.0.1`, `::1`), which exists for plugin development. No request follows a redirect, so the key and the turns never go on to an address Emet's answer names.

### What it does not trust

**Emet's answers.** The plugin acts on little of an answer: the HTTP status, the wait a `Retry-After` header names (an hour at most), two ids, each kept only when it is a UUID, and for the key check the short code of Emet's own "not found". Nothing in an answer is executed, and no text from one is shown to Claude. Of an error, the log and `status.json`, which `/emet:status` reads, keep the HTTP status and a short code of lowercase letters and underscores, 40 characters at most. The error message is not kept.

**The session's text.** The plugin sends it and never acts on it. Nothing in a prompt, an answer or a tool result is executed or used as an address. The hook input itself, the session id and the transcript path, comes from Claude Code and is trusted.

### What is not a boundary

- **Another process running as you.** It can read the transcript and the data folder, queued turns included, and it can change the plugin's files. On macOS and Linux the plugin sets its data folder and files to owner-only, which keeps other users out. On Windows the plugin sets no access rules, so the folder is as private as the folder it sits in, normally your user profile. None of this stops a process that already runs as you.
- **Whoever sets Claude Code's environment.** Options reach the plugin through it. It cannot name another host. It can name a program listening on your own machine, which then receives the key and the turns. [docs/enterprise.md](docs/enterprise.md) says how managed settings close this.
- **Redaction.** `sanitized` removes the secrets its patterns know. Use `metadata` where that is not enough.

## Scope

In scope: anything in this repository that breaks the trust model above. For example a way to make the plugin send the key or a turn anywhere but Emet, send content in `metadata` mode, leak the key into a file or the conversation, read a file other than the ones listed above, write outside its data folder, block or break a session, or let text from one of Emet's answers reach the conversation.

Out of scope: secrets that the `sanitized` redaction patterns do not recognize. Redaction is best effort and documented as such; use `metadata` mode where that matters. Suggestions for new patterns are welcome as ordinary issues, without the secret. Also out of scope is anything that needs a process already running as the user, or control of Claude Code's environment, as described above.

## Supported versions and pinning

Security fixes go into the latest release. An install with no ref follows the `main` branch, which can be ahead of the latest release.

- **Pin a release tag.** Add the marketplace as `FUSEis/emet-plugin#vX.Y.Z`, or set `"ref": "vX.Y.Z"` on the marketplace source in managed settings, as [docs/enterprise.md](docs/enterprise.md) shows. Move the pin forward when a release notes a security fix.
- **Pin a commit for a stricter setup.** A tag can be moved; a commit cannot. Set `ref` on the marketplace source to the full commit SHA of the release you reviewed. `git ls-remote https://github.com/FUSEis/emet-plugin "refs/tags/vX.Y.Z^{}" "refs/tags/vX.Y.Z"` prints it; when two lines come back, the commit is the one ending in `^{}`. This needs Claude Code 2.1.324 or later on every machine. The plugin is a folder of this repository, so pinning the marketplace pins the plugin's code with it.

Claude Code's [marketplace reference](https://code.claude.com/docs/en/plugins/marketplace-reference) describes both forms.
