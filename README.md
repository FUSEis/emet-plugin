# Emet plugins

[Emet](https://emet.so) checks the claims an AI agent makes against the evidence behind them: the tool results, files and data the agent actually received. These plugins send an agent's turns to Emet from the tools where the agent runs, with nothing to change in your code.

| Host | Folder | Status |
| --- | --- | --- |
| Claude Code | [`claude/`](claude/) | Available |
| Codex | | Planned |
| Hermes | | Planned |

## Install with your agent

Paste this into Claude Code:

```text
Install the Emet plugin for Claude Code from https://github.com/FUSEis/emet-plugin.

It needs:
- Team ID (required): the External ID of my team in Emet, under Teams in the sidebar.
  The plugin sends it as `tenant_ref`.
- Emet key (required, secret): from my Emet application, under Applications, then
  Manage keys. I enter it myself.
- Capture mode (optional): `sanitized` by default, which sends prompts, answers and tool
  results with secrets redacted. Keep it unless I ask; `metadata` sends no content.

1. Ask me for my Team ID, and nothing else.
2. Run `claude plugin marketplace add FUSEis/emet-plugin`.
3. Run `claude plugin install emet@emet-plugin --config tenant_ref=<Team ID>`.
   It reports options that are not set yet. That is expected: only the key is left.
4. Never ask me for the key, and never read, print or store it. Tell me to:
   - run `/plugin configure emet@emet-plugin`, type the key into the Emet key field and save;
   - restart Claude Code;
   - run `/emet:setup` in the new session to check that Emet accepts the key.
```

The key is typed only into Claude Code's own configuration dialog, which keeps it in Claude Code's secure credential store (the Keychain on macOS), so it never appears in the conversation.

## Install by hand

In Claude Code:

```text
/plugin marketplace add FUSEis/emet-plugin
/plugin install emet@emet-plugin
/plugin configure emet@emet-plugin
```

Enter your Emet key (Applications, then Manage keys, in Emet) and your team's External ID (Teams in Emet), restart Claude Code, then run `/emet:setup` to check the connection.

For a whole organization, an administrator can install and configure the plugin for every developer through managed settings: see [docs/enterprise.md](docs/enterprise.md).

## What it sends

Each finished turn becomes one trace in Emet: the prompt, every model call with its token counts, every tool call with its arguments and result, and every subagent the turn launched. Secrets are redacted before anything leaves the machine, and images are replaced by a placeholder. [claude/README.md](claude/README.md) lists exactly what is sent, the three capture modes, and how to turn it off.

## Repository layout

```text
.claude-plugin/marketplace.json   the marketplace Claude Code installs from
claude/                           the Claude Code plugin (self-contained)
  .claude-plugin/plugin.json      manifest and options
  hooks/hooks.json                the hooks it registers
  scripts/                        the hook script, Python standard library only
  skills/                         /emet:setup and /emet:status
docs/                             administrator and design documentation
tests/                            tests, run in CI on Linux, macOS and Windows
```

Each plugin folder is self-contained, because Claude Code installs only the plugin's own folder.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md). Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md), not in public issues.

## License

[Apache License 2.0](LICENSE).
