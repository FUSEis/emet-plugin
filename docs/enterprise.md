# Rolling out Emet to an organization

An administrator can install and configure the Emet plugin for every developer at once, through Claude Code's [managed settings](https://code.claude.com/docs/en/plugins/org). Developers then get the plugin at their next session start, already configured, with nothing to type.

## 1. Pick a version

Pin a release tag so that a new version reaches developers only when you move the pin. Releases and their notes are on the [releases page](https://github.com/FUSEis/emet-plugin/releases); each one lists what changed in what the plugin sends.

## 2. Add the managed settings

Deliver these through the Claude admin console (server-managed settings), MDM, or the `managed-settings.json` file:

```json
{
  "extraKnownMarketplaces": {
    "emet-plugin": {
      "source": { "source": "github", "repo": "FUSEis/emet-plugin", "ref": "v0.1.0" },
      "autoUpdate": false
    }
  },
  "enabledPlugins": {
    "emet@emet-plugin": true
  },
  "pluginConfigs": {
    "emet@emet-plugin": {
      "options": {
        "tenant_ref": "your-emet-team-id",
        "capture_mode": "sanitized"
      }
    }
  }
}
```

- `enabledPlugins: true` in managed settings turns the plugin on for everyone, and developers cannot turn it off.
- Options in managed `pluginConfigs` take precedence over a developer's own, so the team and capture mode cannot be changed locally. A project's `.claude/settings.json` cannot set `pluginConfigs` at all, so a cloned repository cannot redirect traces elsewhere.
- If you use `strictKnownMarketplaces`, add `{ "source": "github", "repo": "FUSEis/emet-plugin", "ref": "v0.1.0" }` to the allowlist. An allowlist entry matches only the same `ref`.

## 3. The key

The Emet key is a sensitive option. Each developer enters it once with `/plugin configure emet@emet-plugin`, which stores it in their system keychain. Until a key is set, the plugin sends nothing and `/emet:status` says so.

Use one key per team, issued from the Emet application's **Keys** page, so that a key can be revoked without affecting other teams.

## 4. Check it

On a developer machine, after a session start, `/emet:status` should show the team, the capture mode you set and `key_check: accepted`. Turns appear in Emet under **Conversations** once they are checked.

## Choosing a capture mode

| Mode | Use when | Trade-off |
| --- | --- | --- |
| `sanitized` | Default. Developers work with ordinary code and data. | Pattern-based redaction catches known secret formats, not every secret. |
| `full` | Sessions cannot contain secrets (sandboxes, demo data). | Nothing is redacted. |
| `metadata` | Content must never leave the machine. | Emet reports usage and cost, but cannot check claims. |

Images and binary content are never sent in any mode.

## What your security review will want

- The hook is one Python package in [`claude/scripts/`](../claude/scripts/), standard library only, a few hundred lines. It runs with the developer's permissions, like any hook.
- It sends only to the configured `endpoint`, and only over HTTPS (plain HTTP is accepted for `localhost` alone).
- It reads the session transcript, saved tool outputs and subagent transcripts in Claude Code's own project folder, and nothing outside it.
- It writes only to its own data folder, with owner-only permissions. The status file and log never hold content or the key.
- [claude/README.md](../claude/README.md#what-is-sent) lists every field it sends in each mode.
