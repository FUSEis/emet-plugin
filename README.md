# Emet plugins

Plugins that send coding-agent turns to [Emet](https://emet.so), so the claims an agent makes can be checked against the tool results that produced them.

| Folder | Host | Status |
|---|---|---|
| `claude/` | Claude Code | Spike (EM-331) |

Each plugin folder is self-contained, because Anthropic's directory installs only the plugin folder.

## Try the Claude Code plugin locally

```bash
export EMET_ENDPOINT=http://localhost:8031 EMET_API_KEY=... EMET_TENANT_REF=engineering
claude --plugin-dir ./claude
```

Or install it from this repo as a marketplace:

```bash
claude plugin marketplace add ./
claude plugin install emet@emet-plugins
```

The hook log is in the plugin data directory, for example `~/.claude/plugins/data/emet-inline/emet.log` when loaded with `--plugin-dir`.
