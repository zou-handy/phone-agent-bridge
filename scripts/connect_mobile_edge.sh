#!/usr/bin/env bash
# connect_mobile_edge.sh: Wake mobile Microsoft Edge in top-right mini window and map CDP to 127.0.0.1:9222

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${CDP_PORT:-9222}"
PKG="com.microsoft.emmx"

# 1. Wake Edge in top-right mini window so its unix socket is active and never background-frozen
"$SCRIPT_DIR/launch-mini" "$PKG/com.microsoft.ruby.Main"

sleep 0.6

# 2. Discover @chrome_devtools_remote_<PID> or @chrome_devtools_remote in /proc/net/unix
PID=$(adb shell "pidof $PKG" | tr -d '\r' | awk '{print $1}')
SOCK_CANDIDATES=()
if [[ -n "$PID" ]]; then
  SOCK_CANDIDATES+=("chrome_devtools_remote_${PID}")
fi
SOCK_CANDIDATES+=("chrome_devtools_remote")

for SOCK in "${SOCK_CANDIDATES[@]}"; do
  adb forward --remove "tcp:${PORT}" >/dev/null 2>&1 || true
  if adb forward "tcp:${PORT}" "localabstract:${SOCK}" >/dev/null 2>&1; then
    VER=$(curl -s --max-time 2 "http://127.0.0.1:${PORT}/json/version" || true)
    if [[ "$VER" == *"$PKG"* ]] || [[ "$VER" == *"webSocketDebuggerUrl"* ]]; then
      echo "[connect_mobile_edge] Connected $PKG via @$SOCK -> http://127.0.0.1:${PORT}"
      echo "$VER"
      exit 0
    fi
  fi
done

echo "[connect_mobile_edge] Failed to handshake with $PKG on port ${PORT}." >&2
exit 1
