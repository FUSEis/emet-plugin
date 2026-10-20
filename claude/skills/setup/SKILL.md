---
name: setup
description: Connect the Emet plugin to an Emet account (key and team) and confirm that turns reach Emet. Use when the user wants to set up, configure, connect or reconnect Emet, or change its capture mode.
---

# Set up Emet

Emet checks the claims in each answer against the tool results behind them. This plugin sends each finished turn to Emet in the background. It needs two things: an **Emet key** and a **team ID**.

## Rules

- Never ask the user to paste the Emet key into the chat, and never print, echo or store it yourself. The key is entered only in Claude Code's own configuration dialog, which keeps it in the system keychain.
- Do not edit `settings.json` files to add the key.

## Steps

1. Read `${CLAUDE_PLUGIN_DATA}/status.json`. If it does not exist, the plugin has not run in a session yet; continue with step 2.
2. If `problem` says the key or team is missing, or the user wants to change them, tell the user to run this command in Claude Code and fill in the fields:

   ```
   /plugin configure emet@emet-plugin
   ```

   - **Emet key**: from their Emet application's **Keys** page.
   - **Team ID**: the external ID of their Emet team.
   - **Capture mode**: leave `sanitized` unless they have a reason (see below).

   Then they restart Claude Code, because options apply at startup.
3. After the restart, read `status.json` again and report `key_check`:
   - `accepted`: done. Turns from this session on appear in Emet under **Conversations** once they are checked.
   - `rejected`: the key is wrong or was revoked. Ask them to issue a new key in Emet and run step 2 again.
   - `unreachable`: Claude Code could not reach `endpoint`. Check the network, a proxy, or the endpoint value.
4. If the organization manages Claude Code centrally, an administrator can set these options for everyone; point them to `docs/enterprise.md` in https://github.com/FUSEis/emet-plugin.

## Capture modes

- `sanitized` (default): sends prompts, answers, tool arguments and tool results, with secrets such as API keys, tokens and private keys replaced by `[REDACTED:kind]`.
- `full`: sends content as is. Use only where secrets cannot appear.
- `metadata`: sends model names, token counts, tool names and timings, but no content. Emet cannot check claims in this mode.

In every mode, images and other binary content are replaced by a short placeholder.
