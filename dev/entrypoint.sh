#!/bin/sh
# Forwards localhost in the container to the Emet on the host, then starts Claude Code with the plugin.
# The plugin sends only to app.emet.so or to localhost, so the host's Emet has to answer on localhost here.
set -eu

port="${EMET_PORT:-8000}"
socat "TCP-LISTEN:${port},bind=127.0.0.1,fork,reuseaddr" "TCP:host.docker.internal:${port}" &

exec claude --plugin-dir /plugin "$@"
