#!/usr/bin/env sh
# Safe PID-file teardown for Classroom Portal MCP Hub & Tunnel.
# Avoids `pkill -f cloudflared` accidentally matching the caller's own shell command line (SIGTERM 143).
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$SCRIPT_DIR/hub_server.py" --stop "$@"
