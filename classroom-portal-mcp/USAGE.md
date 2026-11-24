# Classroom Portal MCP — Engineering Usage Reference

## Directory Layout
- `hub_server.py`: Zero-dependency Python 3.12 stdlib Hub (`POST /mcp`, `/files/<token>`, OAuth 2.0 RFC 8414/9728/7591 + PKCE `S256`, 6-digit PIN gate on `/oauth/authorize` and `/pair`).
- `limb/classroom_limb.ps1`: Pure ASCII + CRLF Windows PowerShell 5.1+ outbound Limb (`ReadToEndAsync()`, UTF-8 Base64 `-EncodedCommand`, 4 background jobs, zero-BOM binary file I/O, auto-cleanup on exit).
- `limb/start_classroom_portal.bat`: Double-click Windows launcher.
- `limb/limb_posix.py`: Linux / macOS outbound Limb.
- `cloudflare/worker.js` & `cloudflare/wrangler.toml`: Cloudflare Workers + Durable Objects edition (bind a custom domain for Mainland China `:443` direct access).
- `test_e2e.py`: Automated end-to-end integration test suite.

## Commands

### 1. Start Hub Server (Local / VPS / Phone `:443` Tunnel)
```bash
python3 /root/classroom-portal-mcp/hub_server.py --host 127.0.0.1 --port 8787 --public-url https://your-custom-domain.example.com
```

### 2. Classroom Windows PC (Pure Memory, Zero Installation)
Press `Win + R` on the classroom PC:
```powershell
powershell -NoP -Ep Bypass -c "irm https://your-custom-domain.example.com/go | iex"
```
Or double-click `limb\start_classroom_portal.bat` (set `PORTAL_HUB_URL` or enter URL when prompted).
The window displays a one-time **6-digit PIN** (`000-000`). Closing the window terminates child processes and destroys the Hub room immediately.

### 3. Connect AI Client
- **Web AI Connectors (Claude Web Custom Connectors / ChatGPT Developer Mode)**:
  - Add Remote MCP URL: `https://your-custom-domain.example.com/mcp`
  - OAuth popup opens `/oauth/authorize` -> enter the 6-digit PIN from the classroom screen.
- **CLI / Mobile Agent Direct Pairing (`agy` / `dsh-Go` / `Claude Code`)**:
  ```bash
  curl -fsSL -X POST https://your-custom-domain.example.com/pair \
    -H "Content-Type: application/json" \
    -d '{"pin":"123456"}'
  ```
  Returns `one_paste_mcp_url` (`https://.../mcp/cp_at_...`) and `bearer_token`.

### 4. Exposed 5 MCP Tools
- `run_command` (`command`, `cwd`, `shell`, `timeout_sec`)
- `start_command` (`command`, `cwd`, `shell`)
- `read_command` (`command_id`, `offset`)
- `stop_command` (`command_id`, `force`)
- `file_transfer_info` (returns `GET / PUT / DELETE /files/<token>/?path=...`)

### 5. Run End-to-End Verification
```bash
python3 /root/classroom-portal-mcp/test_e2e.py
```
