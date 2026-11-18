#!/usr/bin/env bash
# connect_mobile_edge.sh: Wake mobile Microsoft Edge (`com.microsoft.emmx`) in top-right mini window (`state=2`)
# and map CDP (`127.0.0.1:9222 -> localabstract:chrome_devtools_remote`) for logged-in session & extension reuse.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${CDP_PORT:-9222}"
PKG="com.microsoft.emmx"

if [[ -x "$SCRIPT_DIR/rsh" ]]; then
  RSH="$SCRIPT_DIR/rsh"
elif command -v rsh >/dev/null 2>&1; then
  RSH="rsh"
else
  RSH="adb shell"
fi

TARGET=$(adb devices | awk '/(127\.0\.0\.1:5555|emulator-5554)[[:space:]]+device/{print $1; exit}')
if [[ -z "$TARGET" ]]; then
  adb connect 127.0.0.1:5555 >/dev/null 2>&1 || true
  TARGET="127.0.0.1:5555"
fi

# 1. Wake Edge in top-right mini window (`state=2`) so its renderer/unix socket is active and never background-frozen
"$SCRIPT_DIR/launch-mini" "$PKG/com.microsoft.ruby.Main" "${1:-}" >/dev/null 2>&1 || true
sleep 0.6

# 2. Discover @chrome_devtools_remote_<PID> or @chrome_devtools_remote
PID=$($RSH "pidof $PKG 2>/dev/null || true" | tr -d '\r' | awk '{print $1}')
SOCK_CANDIDATES=()
if [[ -n "$PID" ]]; then
  SOCK_CANDIDATES+=("chrome_devtools_remote_${PID}")
fi
SOCK_CANDIDATES+=("chrome_devtools_remote")

for SOCK in "${SOCK_CANDIDATES[@]}"; do
  adb -s "$TARGET" forward --remove "tcp:${PORT}" >/dev/null 2>&1 || true
  if adb -s "$TARGET" forward "tcp:${PORT}" "localabstract:${SOCK}" >/dev/null 2>&1; then
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
