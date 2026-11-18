#!/usr/bin/env bash
# connect_mobile_chrome_dev.sh: Connect dedicated clean Agent sandbox browser Chrome Dev (`com.chrome.dev`)
# Maps `127.0.0.1:9223 -> localabstract:chrome_dev_remote` in 100% parallel with user's Edge (`9222`),
# zero extension DOM pollution, and supports `--close` to shut down the mini window cleanly after tests.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${CDP_DEV_PORT:-9223}"
PKG="com.chrome.dev"

if [[ -x "$SCRIPT_DIR/rsh" ]]; then
  RSH="$SCRIPT_DIR/rsh"
elif command -v rsh >/dev/null 2>&1; then
  RSH="rsh"
else
  RSH="adb shell"
fi

if [[ "${1:-}" == "--close" ]]; then
  $RSH "am force-stop $PKG >/dev/null 2>&1 || true"
  adb forward --remove "tcp:${PORT}" >/dev/null 2>&1 || true
  echo "[connect_mobile_chrome_dev] Closed Chrome Dev sandbox cleanly."
  exit 0
fi

TARGET=$(adb devices | awk '/(127\.0\.0\.1:5555|emulator-5554)[[:space:]]+device/{print $1; exit}')
if [[ -z "$TARGET" ]]; then
  adb connect 127.0.0.1:5555 >/dev/null 2>&1 || true
  TARGET="127.0.0.1:5555"
fi

# Configure isolated abstract socket `chrome_dev_remote` so it never collides with Edge's `@chrome_devtools_remote`
$RSH "printf 'chrome --remote-debugging-socket-name=chrome_dev_remote --no-first-run --disable-fre\n' > /data/local/tmp/chrome-command-line && chmod 644 /data/local/tmp/chrome-command-line && am set-debug-app --persistent $PKG >/dev/null 2>&1 || true"

try_connect_dev() {
  adb -s "$TARGET" forward "tcp:${PORT}" "localabstract:chrome_dev_remote" >/dev/null 2>&1 || return 1
  local ver
  ver=$(curl -s --max-time 1.5 "http://127.0.0.1:${PORT}/json/version" || true)
  if [[ "$ver" == *"$PKG"* ]] || [[ "$ver" == *"webSocketDebuggerUrl"* ]]; then
    echo "[connect_mobile_chrome_dev] Connected $PKG -> http://127.0.0.1:${PORT}"
    echo "$ver"
    return 0
  fi
  return 1
}

DEV_PID=$($RSH "pidof $PKG 2>/dev/null || true" | tr -d '\r' | awk '{print $1}')
if [[ -n "$DEV_PID" ]] && try_connect_dev; then
  exit 0
fi

URL_ARG="${1:-about:blank}"
"$SCRIPT_DIR/launch-mini" "$PKG" "$URL_ARG" >/dev/null 2>&1 || true
sleep 0.8

if try_connect_dev; then
  exit 0
fi

echo "[connect_mobile_chrome_dev] Failed to handshake with $PKG on 127.0.0.1:${PORT}." >&2
exit 1
