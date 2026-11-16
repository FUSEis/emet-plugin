# Development environment

A Docker container that runs Claude Code with your working copy of the plugin, against an Emet running on your machine. It is separate from any Claude Code already installed on the machine, including one that has the released plugin and sends to `app.emet.so`.

| | Claude Code on the machine | This container |
| --- | --- | --- |
| Claude Code home | `~/.claude` | Its own Docker volume. `~/.claude` is never mounted |
| Plugin | Whatever is installed there | `./claude` from this checkout, read-only |
| Emet | `https://app.emet.so` | The Emet on this machine |
| Key | The one saved there | The one in `dev/.env` |
| Turns sent | Sessions run on the machine | Only sessions run in the container |

The session works in a scratch folder, `/workspace`, kept in a Docker volume. Your own projects are not mounted.

## Start it

You need Docker, and an Emet running on this machine (port 8000 unless you change `EMET_PORT`).

1. Copy the settings file and fill it in. The Emet key and Team ID come from the local Emet.

   ```bash
   cp dev/.env.example dev/.env
   ```

2. Start a session. The first run builds the image.

   ```bash
   docker compose -f dev/compose.yaml run --rm claude
   ```

3. Sign in if you set no token or API key in `dev/.env`: run `/login` in the session. The sign-in is kept in the container's own volume.

4. Run `/emet:status` to see where turns are going. It should name `http://localhost:8000`.

Arguments after `claude` go to Claude Code, for example `docker compose -f dev/compose.yaml run --rm claude --resume`.

## How it works

- **The session is interactive.** It is a normal Claude Code session in your terminal, so hooks behave as they do for a user.
- **The plugin is loaded with `--plugin-dir`** and gets its options from the `CLAUDE_PLUGIN_OPTION_*` variables in `dev/.env`, as described in [CONTRIBUTING.md](../CONTRIBUTING.md). An edit to `./claude` applies to the next session you start.
- **The plugin only sends to `app.emet.so` or to `localhost`.** The container forwards its own `localhost:8000` to port 8000 on your machine, so the plugin's rule is unchanged.
- **The Claude Code version is pinned** in `dev/Dockerfile` and does not update itself. A test keeps it equal to the version CI validates the plugin with.
- **The hook log** is at `/home/node/.claude/plugins/data/emet-inline/emet.log` in the container.

Model requests from the session go to Anthropic under the sign-in you give it, as they do outside the container.

## Reset it

Remove the container's Claude Code home and scratch folder:

```bash
docker compose -f dev/compose.yaml down --volumes
```

Rebuild after changing the pinned version:

```bash
docker compose -f dev/compose.yaml build
```
