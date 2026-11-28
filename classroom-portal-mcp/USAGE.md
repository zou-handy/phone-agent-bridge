# Classroom Portal MCP — Engineering Usage Reference

## Directory Layout
- `hub_server.py`: Zero-dependency Python 3.12 stdlib Hub (`POST /mcp`, `/files/<token>`, OAuth 2.0 RFC 8414/9728/7591 + PKCE `S256`, 6-digit PIN gate on `/oauth/authorize` and `/pair`, dynamic `--public-url auto` header tracking, `--pidfile` & `--stop`).
- `stop_portal.sh`: Safe PID-file teardown wrapper (avoids `pkill -f` subshell self-kill).
- `limb/classroom_limb.ps1`: Pure ASCII + CRLF Windows PowerShell 5.1+ outbound Limb (`ReadToEndAsync()`, UTF-8 Base64 `-EncodedCommand`, 4 background jobs, zero-BOM binary file I/O, auto-cleanup on exit).
- `limb/start_classroom_portal.bat`: Double-click Windows launcher.
- `limb/limb_posix.py`: Linux / macOS outbound Limb.
- `cloudflare/worker.js` & `cloudflare/wrangler.toml`: Cloudflare Workers + Durable Objects edition (bind a custom domain for 24/7 Mainland China `:443` direct access).
- `test_e2e.py`: Automated end-to-end integration test suite.

## Quick Commands

### 1. Start Hub Server + `:443` Tunnel
```bash
# 1) Start Hub (auto-detects public tunnel URL via Host / X-Forwarded-Host)
python3 classroom-portal-mcp/hub_server.py --host 127.0.0.1 --port 8787 \
  --pidfile /tmp/classroom_portal_hub.pid > /tmp/portal_hub.log 2>&1 &

# 2A) Zero-download SSH tunnel (ideal for Cloud Sandboxes / Doubao Work where trycloudflare returns 503):
ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 \
  -R 80:127.0.0.1:8787 nokey@localhost.run > /tmp/portal_tunnel.log 2>&1 < /dev/null &
echo $! > /tmp/classroom_portal_tunnel.pid

# 2B) Or cloudflared Quick Tunnel on residential/campus networks:
cloudflared tunnel --protocol http2 --edge-ip-version 4 --no-autoupdate \
  --url http://127.0.0.1:8787 > /tmp/portal_tunnel.log 2>&1 &
echo $! > /tmp/classroom_portal_tunnel.pid
```

### 2. Classroom Windows PC (Pure Memory, Zero Installation)
Press `Win + R` on each target Windows PC:
```powershell
powershell -NoP -Ep Bypass -c "irm https://<your-tunnel-or-domain>/go | iex"
```
Each window displays an independent one-time **6-digit PIN** (`000-000`). Closing the window terminates child processes and destroys that PC's room immediately (`HTTP 401`).

### 3. Pair & Operate
```bash
curl -fsSL -X POST http://127.0.0.1:8787/pair \
  -H "Content-Type: application/json" \
  -d '{"pin":"743-601"}'
```
Returns `one_paste_mcp_url` (`https://.../mcp/cp_at_...`), `bearer_token`, and `files_base_url`.

### 4. Safe Teardown & E2E Tests
```bash
# Safely stop Hub + Tunnel via PID files (never use bare `pkill -f`)
python3 classroom-portal-mcp/hub_server.py --stop

# Run full E2E verification suite
python3 classroom-portal-mcp/test_e2e.py
```
