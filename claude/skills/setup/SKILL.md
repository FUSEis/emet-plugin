---
name: setup
description: Connect the Emet plugin to an Emet account (key and team) and confirm that turns reach Emet. Use when the user wants to set up, configure, connect or reconnect Emet, or change its capture mode.
---

# Set up Emet

Emet checks the claims in each answer against the tool results behind them. This plugin sends each finished turn to Emet in the background.

## Options

| Option | Required | Where it comes from |
| --- | --- | --- |
| **Team ID** | yes | The **External ID** of the user's team, under **Teams** in Emet. It is sent as `tenant_ref`. |
| **Emet key** | yes, secret | The user's Emet application, under **Applications**, then **Manage keys**. |
| **Capture mode** | no | A picker, `sanitized` by default. See below. |

Right after `claude plugin install`, Claude Code reports options that are not set yet. That is expected: when the Team ID was passed with `--config tenant_ref=...`, only the key is missing, and capture mode has a default.

## Rules

- Never ask the user for the Emet key, and never read, print or store it. The user types it into Claude Code's configuration dialog, which keeps it in Claude Code's secure credential store.
- Do not edit `settings.json` files to add the key or any other option.

## Steps

1. Read `${CLAUDE_PLUGIN_DATA}/status.json`.
   - It does not exist, `problem` says the key or team is missing, or the user wants to change an option: go to step 2.
   - `problem` names an endpoint: something in the environment sets `CLAUDE_PLUGIN_OPTION_ENDPOINT` to a host other than Emet, and the plugin refuses to send. Ask the user to remove it (a shell profile, or an `env` entry in a `.claude/settings.json`) and restart Claude Code.
   - Otherwise go to step 3.
2. Tell the user to run this in Claude Code, fill in the fields and choose **Save configuration**:

   ```
   /plugin configure emet@emet-plugin
   ```

   Options apply at startup, so they then restart Claude Code and run `/emet:setup` again in the new session. This session ends with the restart, so say that plainly.
3. Report `key_check`:
   - `accepted`: Emet accepts the key. This does not check the Team ID. Ask the user to finish one turn, then run `/emet:status`: `last_delivery` should be set, and the conversation should appear in Emet under **Conversations** with their team's name. A conversation with no team means the Team ID matches no External ID; fix it with step 2.
   - `rejected`: Emet does not accept the key. Ask the user to enter it again with step 2, checking for a missing or extra character. If it is still rejected, it is revoked or expired: they mint a new one in Emet.
   - `unreachable`: Claude Code could not reach Emet. Check the network or a proxy.
   - Not there yet: the check runs in the background when a session starts. Wait a few seconds and read `status.json` again.
   - Anything else (for example `unexpected 500`): show it to the user and suggest `/emet:status`.
4. If the organization manages Claude Code centrally, an administrator can install the plugin and set the Team ID and capture mode for everyone; point them to `docs/enterprise.md` in https://github.com/FUSEis/emet-plugin. Each developer still enters the key.

## Capture modes

- `sanitized` (default): sends prompts, answers, tool arguments and tool results, with secrets such as API keys, tokens and private keys replaced by `[REDACTED:kind]`.
- `full`: sends content as is. Use only where secrets cannot appear.
- `metadata`: sends model names, token counts, tool names and timings, but no content. Emet cannot check claims in this mode.

In every mode, images and other binary content are replaced by a short placeholder.
