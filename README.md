# Emet plugins

[Emet](https://emet.so) checks the claims an AI agent makes against the evidence behind them: the tool results, files and data the agent actually received. These plugins send an agent's turns to Emet from the tools where the agent runs, with nothing to change in your code.

| Host | Folder | Status |
| --- | --- | --- |
| Claude Code | [`claude/`](claude/) | Available |
| Codex | | Planned |
| Hermes | | Planned |

## Install with your agent

If your organization rolls Emet out through managed settings, there is nothing to install: the plugin arrives configured at your next session. Otherwise, paste this into Claude Code:

```text
Install the Emet plugin for Claude Code from https://github.com/FUSEis/emet-plugin
and walk me through connecting it.

What it needs:
- Team ID (required): the External ID of my team in Emet, under Teams in the sidebar.
- Emet key (required, secret): my team's key, from Applications, then Manage keys,
  in Emet. I type it in myself.
- My Emet email (optional): only if I don't sign in to Claude with the email of my
  Emet account, or I use Claude through an API key, Bedrock, Vertex AI or a gateway.
- Capture mode (optional): keep the default, `sanitized`, unless I ask.

Rules: never ask me for the key, and never read, print or store it. Do not edit
settings.json files.

1. Run `claude plugin list`. If `emet@emet-plugin` is installed, note its
   version, run `claude plugin marketplace update emet-plugin` and
   `claude plugin update emet@emet-plugin`. If it was older than 0.1.3, skip
   to step 4 (the key has to be entered again). Otherwise skip to the last
   bullet of step 4.
2. Run `claude auth status --text`. If I'm signed in with a Claude account, show
   me its email and ask whether it is the email of my Emet account. If it is not,
   or I'm not signed in with a Claude account, ask me for my Emet email.
3. Ask me for my Team ID, then run:
   - `claude plugin marketplace add FUSEis/emet-plugin`
   - `claude plugin install emet@emet-plugin --config tenant_ref=<Team ID>`,
     adding `--config emet_email=<email>` if I gave one.
   It reports options that are not set yet. That is expected: the key is left.
4. Tell me, in one short list, to:
   - run `/plugin configure emet@emet-plugin`, type the key into the Emet key
     field and choose Save configuration;
   - restart Claude Code (this conversation ends there);
   - in the new session, run `/emet:setup`, which checks the key and, after my
     first turn, links its report in Emet.
```

The key is typed only into Claude Code's own configuration dialog, which keeps it in Claude Code's secure credential store (the Keychain on macOS), so it never appears in the conversation.

## Install by hand

In Claude Code:

```text
/plugin marketplace add FUSEis/emet-plugin
/plugin install emet@emet-plugin
/plugin configure emet@emet-plugin
```

Enter your team's Emet key (Applications, then Manage keys, in Emet) and your team's External ID (Teams in Emet), restart Claude Code, then run `/emet:setup` to check the connection.

## Install for a whole organization

An administrator can install and configure the plugin for every developer at once through Claude Code's managed settings, key included, so developers enter nothing. Each turn is attributed to the developer by the email they sign in to Claude with. See [docs/enterprise.md](docs/enterprise.md).

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
