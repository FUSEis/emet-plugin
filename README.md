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

1. Run `claude plugin marketplace add FUSEis/emet-plugin`.
2. Ask me for my Emet team ID, then run
   `claude plugin install emet@emet-plugin --config tenant_ref=<team ID>`.
3. Do not ask me for my Emet key, and never read, print or store it. Tell me to run
   `/plugin configure emet@emet-plugin` in Claude Code, enter the key there,
   and restart Claude Code.
4. After I restart, run `/emet:setup` and tell me whether the key was accepted.
```

The key is typed only into Claude Code's own configuration dialog, which keeps it in the system keychain, so it never appears in the conversation.

## Install by hand

In Claude Code:

```text
/plugin marketplace add FUSEis/emet-plugin
/plugin install emet@emet-plugin
/plugin configure emet@emet-plugin
```

Enter your Emet key and team ID, restart Claude Code, then run `/emet:setup` to check the connection.

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
