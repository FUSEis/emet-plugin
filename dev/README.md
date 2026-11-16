# Development environment

A Docker container that runs Claude Code with your working copy of the plugin, against an Emet running on your machine. It is separate from any Claude Code already installed on the machine, including one that has the released plugin and sends to `app.emet.so`.

It keeps the two apart; it is not a security sandbox. The session has network access, can reach services on your machine, and holds the keys from `dev/.env` in its environment.

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

2. Start a session. `--build` rebuilds the image when anything under `dev/` changed, and is quick when nothing did.

   ```bash
   docker compose -f dev/compose.yaml run --build --rm claude
   ```

3. Sign in if you set no token or API key in `dev/.env`: the first run asks you to, and `/login` in the session does it again later. The sign-in is kept in the container's own volume.

4. Run `/emet:status` to see where turns are going. It should name `http://localhost:8000`.

Arguments after `claude` go to Claude Code, for example `docker compose -f dev/compose.yaml run --build --rm claude --resume`.

## How it works

- **The session is interactive.** It is a normal Claude Code session in your terminal, so hooks fire as they do for a user. When the session ends, the container waits up to 30 seconds for the plugin to finish sending before it stops.
- **The plugin is loaded with `--plugin-dir`** and gets its options from the `CLAUDE_PLUGIN_OPTION_*` variables in `dev/.env`, as described in [CONTRIBUTING.md](../CONTRIBUTING.md). An edit to `./claude` applies to the next session you start.
- **The plugin only sends to `app.emet.so` or to `localhost`.** The container forwards its own `localhost:8000` to port 8000 on your machine, so the plugin's rule is unchanged.
- **The Claude Code version is pinned** in `dev/Dockerfile` and does not update itself. A test keeps it equal to the version CI validates the plugin with.
- **The hook log** is at `/home/node/.claude/plugins/data/emet-inline/emet.log` in the container.
- **When the local Emet is not running** the session still starts. The plugin logs `unreachable` and queues the turns, and the forward's own errors go to `/tmp/emet-forward.log` in the container.

## On Linux

The container reaches your machine through Docker's bridge address, not through the machine's own `localhost`. A local Emet that listens only on `127.0.0.1` is not reachable that way, and `/emet:status` shows it as unreachable. Start Emet on the bridge address (often `172.17.0.1`) for this. Do not use `0.0.0.0`, which opens a development Emet to your whole network. Docker Desktop on macOS needs none of this.

Model requests from the session go to Anthropic under the sign-in you give it, as they do outside the container.

## Reset it

Remove the container's Claude Code home and scratch folder:

```bash
docker compose -f dev/compose.yaml down --volumes
```

All checkouts of this repository share one image and one set of volumes. Rebuild without starting a session:

```bash
docker compose -f dev/compose.yaml build
```
