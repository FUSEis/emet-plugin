---
name: setup
description: Connect the Emet plugin to an Emet account (key, team and email) and confirm that turns reach Emet. Use when the user wants to set up, configure, connect or reconnect Emet, or change its capture mode.
---

# Set up Emet

Emet checks the claims in each answer against the tool results behind them. This plugin sends each finished turn to Emet in the background.

## Options

| Option | Required | Where it comes from |
| --- | --- | --- |
| **Team ID** | yes | The **External ID** of the user's team, under **Teams** in Emet. It is sent as `tenant_ref`. |
| **Emet key** | yes, secret | The team's key, from the Emet application, under **Applications**, then **Manage keys**. |
| **Your Emet email** | no | Blank by default: the plugin sends the email the user signs in to Claude with, so Emet attributes their turns to them. Set it when they use an API key, Bedrock, Vertex AI or a gateway, or when their Emet email differs. |
| **Capture mode** | no | A picker, `sanitized` by default. See below. |

Right after `claude plugin install`, Claude Code reports options that are not set yet. That is expected: when the Team ID was passed with `--config tenant_ref=...`, only the key is missing, and the others are optional.

In an organization that manages Claude Code centrally, an administrator may have set the key, the Team ID and the capture mode for everyone. Then `status.json` shows no `problem` once a session has started, and there is nothing to configure: do not ask the user to enter a key, since a key they save takes precedence over the administrator's.

## Rules

- Never ask the user for the Emet key, and never read, print or store it. The user types it into Claude Code's configuration dialog, which keeps it in Claude Code's secure credential store.
- Do not edit `settings.json` files to add the key or any other option.

## Steps

1. Read `${CLAUDE_PLUGIN_DATA}/status.json`.
   - It does not exist: no session has started with the plugin yet. Ask the user to restart Claude Code and run `/emet:setup` again; if it is still missing, go to step 2.
   - `problem` says the key or team is missing, or the user wants to change an option: first ask whether their organization installs Claude Code plugins for them (managed settings).
     - Managed: do not go to step 2. The key, the Team ID and the capture mode come from their administrator, and a key the user saves would take precedence over the administrator's for good. Ask them to restart Claude Code; if `problem` is still set, they ask their administrator to check the managed settings (the key under `CLAUDE_PLUGIN_OPTION_EMET_KEY`, the Team ID in `pluginConfigs`).
     - Managed, and their turns need an email (step 3 shows `enduser_ref` empty or wrong): **Your Emet email** is the one option a managed user sets themselves. Do not send them to the configuration dialog, which also shows the key field. Ask them for their Emet email, take it only from their reply (never from `status.json`), and if it is a plain address (letters, digits, `.` `_` `%` `+` `-` and one `@`, no quotes, spaces or backslashes), run it in place of the example:

       ```
       echo '{"emet_email": "you@example.com"}' | claude plugin configure emet@emet-plugin --values-stdin
       ```

       This saves only the email and leaves every other option as it is. Otherwise ask them to run it themselves. They then restart Claude Code.
     - Not managed: go to step 2.
   - `problem` names an endpoint: `CLAUDE_PLUGIN_OPTION_ENDPOINT` points at a host other than Emet, and the plugin refuses to send. It comes from one of these; ask the user to remove it and restart Claude Code:
     - an `endpoint` saved by version 0.1.0, under `pluginConfigs` > `emet@emet-plugin` > `options` in `~/.claude/settings.json` (the user deletes that one line);
     - an `env` entry in a `.claude/settings.json`;
     - an export in a shell profile.
   - Otherwise go to step 3.
2. Tell the user to run this in Claude Code, fill in the fields and choose **Save configuration**:

   ```
   /plugin configure emet@emet-plugin
   ```

   Options apply at startup, so they then restart Claude Code and run `/emet:setup` again in the new session. This session ends with the restart, so say that plainly.
3. Report `key_check`:
   - `accepted`: Emet accepts the key. This does not check the Team ID or the email. Then:
     - Say who turns are sent as: `enduser_ref`, and `enduser_source` (`claude account` or `setting`). If `enduser_ref` is empty, turns arrive with no person: give `enduser_note` (why no email was found) and suggest setting **Your Emet email** with step 2 (managed users: the command in step 1, second managed case).
     - If `last_delivery` has a `report_url`, give it to the user: it opens the report of their latest turn in Emet, for a member of their Emet organization. If they cannot open it, their Emet administrator invites the email in `enduser_ref`. Without a link, ask them to finish one turn and run `/emet:status`.
     - In Emet, the conversation should show their team's name and their name. No team means the Team ID matches no External ID: fix it with step 2, or, when the administrator sets it, ask them to. No person means the email matches no member of their Emet organization: set **Your Emet email** with step 2 (managed users: the command in step 1, second managed case), or ask their Emet administrator to invite the email in `enduser_ref`.
     - The link opens an empty list until Emet has checked the turn; try again shortly.
   - `rejected`: Emet does not accept the key; it is mistyped, revoked or expired.
     - If their organization manages Claude Code, the key comes from their administrator: ask the user to restart Claude Code, which picks up a key the administrator has replaced. If it is still rejected, they ask their administrator for the current key. A key they saved themselves takes precedence over the administrator's and cannot be cleared, so if they saved one, they enter the current team key with step 2.
     - Otherwise ask the user to enter it again with step 2, checking for a missing or extra character. If it is still rejected, someone who manages keys in their Emet organization issues a new one.
   - `unreachable`: Claude Code could not reach Emet. Check the network or a proxy.
   - Not there yet: the check runs in the background when a session starts. Wait a few seconds and read `status.json` again.
   - Anything else (for example `unexpected 500`): show it to the user and suggest `/emet:status`.
4. If the user sets up Emet for other people too, point them to `docs/enterprise.md` in https://github.com/FUSEis/emet-plugin: an administrator can install and configure the plugin for everyone through managed settings, key included.

## Capture modes

- `sanitized` (default): sends prompts, answers, tool arguments and tool results, with secrets such as API keys, tokens and private keys replaced by `[REDACTED:kind]`.
- `full`: sends content as is, except Emet keys. Use only where secrets cannot appear.
- `metadata`: sends model names, token counts, tool names and timings, but no content. Emet cannot check claims in this mode. The user's email is still sent, to attribute the turn.

In every mode, images and other binary content are replaced by a short placeholder.
