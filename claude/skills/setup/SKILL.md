---
name: setup
description: Connect the Emet plugin to an Emet account (key and team) and confirm that turns reach Emet. Use when the user wants to set up, configure, connect or reconnect Emet, or change its capture mode.
---

# Set up Emet

Emet checks the claims in each answer against the tool results behind them. This plugin sends each finished turn to Emet in the background.

## Options

| Option | Required | Where it comes from |
| --- | --- | --- |
| **Emet key** | yes, secret | The **Keys** page of the user's Emet application. |
| **Team ID** | yes | The **External ID** of the user's team on Emet's **Teams** page. It is sent as `tenant_ref`. |
| **Capture mode** | no | A picker, `sanitized` by default. See below. |

Right after `claude plugin install`, Claude Code reports options that are not set yet. That is expected: when the Team ID was passed with `--config tenant_ref=...`, only the key is missing, and capture mode has a default.

## Rules

- Never ask the user for the Emet key, and never read, print or store it. The user types it into Claude Code's configuration dialog, which keeps it in the macOS Keychain, or in Claude Code's credentials file where there is no keychain.
- Do not edit `settings.json` files to add the key or any other option.

## Steps

1. Read `${CLAUDE_PLUGIN_DATA}/status.json`. If it does not exist, the plugin has not run in a session yet; continue with step 2.
2. If `problem` says the key or team is missing, or the user wants to change an option, tell the user to run this in Claude Code, fill in the fields and choose **Save configuration**:

   ```
   /plugin configure emet@emet-plugin
   ```

   Then they restart Claude Code, because options apply at startup.
3. After the restart, read `status.json` again and report `key_check`:
   - `accepted`: done. Turns from this session on appear in Emet under **Conversations** once they are checked.
   - `rejected`: Emet does not accept the key. It is mistyped, revoked or expired; ask them to issue a new key in Emet and repeat step 2.
   - `unreachable`: Claude Code could not reach Emet. Check the network or a proxy.
4. If the organization manages Claude Code centrally, an administrator can install the plugin and set the Team ID and capture mode for everyone; point them to `docs/enterprise.md` in https://github.com/FUSEis/emet-plugin. Each developer still enters the key.

## Capture modes

- `sanitized` (default): sends prompts, answers, tool arguments and tool results, with secrets such as API keys, tokens and private keys replaced by `[REDACTED:kind]`.
- `full`: sends content as is. Use only where secrets cannot appear.
- `metadata`: sends model names, token counts, tool names and timings, but no content. Emet cannot check claims in this mode.

In every mode, images and other binary content are replaced by a short placeholder.
