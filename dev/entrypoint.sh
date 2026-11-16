#!/bin/sh
# Forwards localhost in the container to the Emet on the host, then starts Claude Code with the plugin.
# The plugin sends only to app.emet.so or to localhost, so the host's Emet has to answer on localhost here.
set -eu

port="${EMET_PORT:-8000}"
# socat reports each refused connection; keep that off the session's terminal
socat "TCP-LISTEN:${port},bind=127.0.0.1,fork,reuseaddr" "TCP:host.docker.internal:${port}" 2>>/tmp/emet-forward.log &

status=0
claude --plugin-dir /plugin "$@" || status=$?

# The plugin sends from detached workers; let them finish before the container stops
waited=0
while [ "${waited}" -lt 30 ] && grep -qs '[e]met_hook.py' /proc/[0-9]*/cmdline; do
    sleep 1
    waited=$((waited + 1))
done

exit "${status}"
