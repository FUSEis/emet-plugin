# Rolling out Emet to an organization

An administrator can install and configure the Emet plugin for every developer at once, through Claude Code's [managed settings](https://code.claude.com/docs/en/plugins/org). Each developer then gets the plugin at their next session start, already configured, with nothing to enter: the team's key, the Team ID and the capture mode come from the managed settings, and each turn is attributed to the developer by the email they sign in to Claude with.

## 1. Prepare Emet

- **The key:** in Emet, open **Applications**, then **Manage keys**, and issue one key for the team. Every developer on the team sends with it.
- **The Team ID:** the **External ID** of the team, under **Teams** in Emet.
- **The developers:** Emet attributes a turn to a person by matching their email against the members of your Emet organization. Invite each developer to Emet with the email they sign in to Claude with. A developer who is not a member still sends turns; they appear under the team, with no person.

## 2. Pick a version

Pin a release tag so that a new version reaches developers only when you move the pin. Releases and their notes are on the [releases page](https://github.com/FUSEis/emet-plugin/releases); each one lists what changed in what the plugin sends.

## 3. Add the managed settings

Deliver these through the Claude admin console (server-managed settings), MDM, or the `managed-settings.json` file:

```json
{
  "extraKnownMarketplaces": {
    "emet-plugin": {
      "source": { "source": "github", "repo": "FUSEis/emet-plugin", "ref": "v0.1.3" },
      "autoUpdate": false
    }
  },
  "enabledPlugins": {
    "emet@emet-plugin": true
  },
  "pluginConfigs": {
    "emet@emet-plugin": {
      "options": {
        "tenant_ref": "your-team-external-id",
        "capture_mode": "sanitized"
      }
    }
  },
  "env": {
    "CLAUDE_PLUGIN_OPTION_EMET_KEY": "your-team-emet-key"
  }
}
```

- `enabledPlugins: true` in managed settings turns the plugin on for everyone, and developers cannot turn it off.
- Options in managed `pluginConfigs` take precedence over a developer's own, so the team and capture mode cannot be changed locally. A project's `.claude/settings.json` cannot set `pluginConfigs` at all, and the plugin refuses any destination other than Emet, so a cloned repository cannot redirect traces elsewhere.
- The key goes in `env` because `pluginConfigs` cannot hold a secret option. Claude Code passes it to the plugin's hooks. A key a developer saved with `/plugin configure` takes precedence over it.
- If you use `strictKnownMarketplaces`, add `{ "source": "github", "repo": "FUSEis/emet-plugin", "ref": "v0.1.3" }` to the allowlist. An allowlist entry matches only the same `ref`.
- For developers on several Emet teams, deliver one profile per group, each with its own Team ID and key.

### What a key in managed settings means

- The managed settings file is readable by the developer on their own machine, and `env` values reach every command Claude Code runs. The key is not kept in the system keychain on this route.
- The Emet key can only send traces to your organization; it cannot read reports or anything else. Anyone holding it could send turns that would appear as your team's.
- To rotate it, issue a new key in Emet, update the managed settings, then revoke the old key once the new settings have reached everyone.

If that does not fit your policy, leave `env` out and have each developer enter the key once with `/plugin configure emet@emet-plugin`, which keeps it in Claude Code's secure credential store (the Keychain on macOS). Everything else in the managed settings stays the same.

## 4. Who ran each turn

The plugin reads the email of the Claude account a developer is signed in to, from Claude Code's own account file (`~/.claude.json`, or the one in `CLAUDE_CONFIG_DIR`), and sends it as `enduser_ref`, which Emet matches to a member of your organization. Nothing is entered.

It cannot do that when Claude Code runs on an API key, Amazon Bedrock, Google Vertex AI or a gateway without a Claude sign-in, since there is no Claude account. Those developers, and anyone whose Claude email differs from their Emet email, set **Your Emet email** once with `/plugin configure emet@emet-plugin`.

## 5. Check it

On one developer machine, after a session start, `/emet:status` should show:

- the team and capture mode you set, and `key_check: accepted`;
- the email being sent and where it came from (the Claude account, or the setting);
- after one finished turn, a `last_delivery` with a link to the conversation in Emet.

The conversation should appear under **Conversations** with your team's name and the developer. A conversation with no team means the Team ID matches no External ID; one with no person means the email matches no member.

## Choosing a capture mode

| Mode | Use when | Trade-off |
| --- | --- | --- |
| `sanitized` | Default. Developers work with ordinary code and data. | Pattern-based redaction catches known secret formats, not every secret. |
| `full` | Sessions cannot contain secrets (sandboxes, demo data). | Nothing is redacted. |
| `metadata` | Content must never leave the machine. | Emet reports usage and cost, but cannot check claims. |

Images and binary content are never sent in any mode.

## What your security review will want

- The hook is one Python package in [`claude/scripts/`](../claude/scripts/), standard library only, a few hundred lines. It runs with the developer's permissions, like any hook.
- It sends only to Emet (`https://app.emet.so`), over HTTPS, and refuses any other destination. The one exception is an Emet on the same machine, for plugin development.
- It reads the session transcript, saved tool outputs and subagent transcripts in Claude Code's own project folder, and nothing outside it, except the signed-in account's email in Claude Code's account file. It runs no other program.
- It writes only to its own data folder, with owner-only permissions. The status file and log never hold content or the key.
- [claude/README.md](../claude/README.md#what-is-sent) lists every field it sends in each mode.
