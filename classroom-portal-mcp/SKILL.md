---
name: classroom-portal-mcp
description: Zero-cable, zero-login HTTPS :443 remote control of public, lab, or borrowed Windows/Linux/macOS computers using a 5-tool Streamable HTTP MCP hub + OAuth 2.0 + 6-digit one-time screen PIN (hub_server.py + free cloudflared Quick Tunnel or Cloudflare Workers). Trigger whenever the user wants an AI Agent (Antigravity, Claude Code, Codex, Cursor, OpenClaw, DSH-Go, or Web AI connectors) to run commands, edit code, query databases, transfer binary files, or drive GUI apps on a restricted/shadow-restored lab PC without plugging in a USB cable or logging personal accounts onto the PC.
---

# Classroom Portal MCP — Zero-Cable, Zero-Residue Lab PC Bridge (`classroom-portal-mcp`)

This skill equips any **CLI, Mobile, Desktop, or Web AI Agent** (**Antigravity**, **Claude Code**, **Codex**, **Cursor**, **OpenClaw**, **DSH-Go**, **Claude Web Custom Connectors**, **ChatGPT Developer Mode**) to connect to a restricted public or lab computer over standard outbound **HTTPS (`:443`)** — **with zero USB cables, zero software installation, zero Windows Firewall popups, and zero account/API-key residue on the target computer**.

> **Deep Engineering Reference**: For root-cause postmortems of every Windows VDI, HTTP/1.1 Keep-Alive, PowerShell encoding, nested VMware, and fixed-ratio GUI automation pitfall behind this skill, read [`WINDOWS_VDI_PITFALLS.md`](./WINDOWS_VDI_PITFALLS.md).

---

## 1. Architecture & Security Boundaries

1. **Strict One-Way Control, Two-Way Data**:
   - **Control is strictly one-way (`Agent -> Target PC`)**: The disposable limb (`classroom_limb.ps1` / `limb_posix.py`) only long-polls `/limb/poll` for tasks and posts results to `/limb/reply`. The target PC has **zero access** to your personal machine, phone files, memory vault, or API keys.
   - **Data & Files flow both ways**: Execute commands (`run_command`, `start_command`, `read_command`, `stop_command`) and move binary/text files losslessly (`GET` / `PUT` / `DELETE /files/<token>/?path=...`).
2. **Multi-Room Isolation & Instant Self-Destruct**:
   - One Hub supports multiple target PCs simultaneously (`activeRooms >= 1`). Each limb receives an isolated `room_id`, a unique 6-digit one-time PIN (immediately rotated upon successful pairing; auto-rotated after 5 failed attempts), and scoped bearer tokens.
   - Closing the target PC's terminal window sends `/limb/disconnect` (or expires after `120s` idle TTL), immediately wiping the room and invalidating all tokens (`HTTP 401`).
3. **Zero-Domain Decentralized Rule (Essential When Sharing With Peers)**:
   - **Default & Recommended (`$0`, No Domain Required)**: Each user runs their own `hub_server.py` + `cloudflared` Quick Tunnel (`https://<random>.trycloudflare.com`). Every user gets an independent ephemeral `:443` URL — zero domain cost, zero shared server maintenance, and zero cross-user correlation on campus/corporate firewalls.
   - **Personal Custom Domain (`cloudflare/worker.js`)**: Reserve custom domains strictly for **single-user personal convenience** (fixed short URL, 24/7 serverless). Never share one personal domain across many lab computers simultaneously.

---

## 2. Standard Operating Procedure (SOP) for the Agent

### Step 1: Start the Local Hub + Free `:443` Quick Tunnel
Ensure `cloudflared` is available (official single binary from Cloudflare Releases, or `winget install Cloudflare.cloudflared` / `brew install cloudflared`), then start a Quick Tunnel to `127.0.0.1:8787` and launch `hub_server.py` with the assigned `--public-url`:

- **Linux / macOS / Android Container (Bash)**:
  ```bash
  cloudflared tunnel --protocol http2 --no-autoupdate --url http://127.0.0.1:8787 > /tmp/cf_portal.log 2>&1 &
  sleep 5
  URL=$(grep -oE 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' /tmp/cf_portal.log | head -n 1)
  python3 hub_server.py --host 127.0.0.1 --port 8787 --public-url "$URL"
  ```
- **Windows Host (PowerShell)**:
  1. Start `cloudflared.exe tunnel --protocol http2 --no-autoupdate --url http://127.0.0.1:8787 --logfile cf_portal.log` in the background.
  2. Extract the `https://<words>.trycloudflare.com` URL from `cf_portal.log`.
  3. Run `python hub_server.py --host 127.0.0.1 --port 8787 --public-url "<URL>"`.
  4. Verify readiness: `curl -fsSL "<URL>/health"` returns `{"ok": true, ...}`.

### Step 2: Bootstrap the Target Lab PC (Pure RAM, Zero Install)
Give the user the generated one-liner for the target computer:

- **Windows Target PC (`Win + R` or PowerShell)**:
  ```powershell
  powershell -NoP -Ep Bypass -c "irm https://<your-tunnel>.trycloudflare.com/go | iex"
  ```
  *(Or double-click `limb\start_classroom_portal.bat` and paste the Hub URL.)*
- **Linux / macOS Target PC**:
  ```bash
  curl -fsSL https://<your-tunnel>.trycloudflare.com/go.py | python3 -
  ```

The target PC prints a one-time **6-digit PIN** (e.g., `743-601`) and triggers **no Windows Defender Firewall popup** because it makes strictly outbound `:443` connections.

### Step 3: Pair With the 6-Digit PIN
Ask the user for the 6-digit PIN shown on the target screen:

- **CLI / Mobile / Desktop Agent Pairing**:
  ```bash
  curl -fsSL -X POST "http://127.0.0.1:8787/pair" \
    -H "Content-Type: application/json" \
    -d '{"pin":"743-601"}'
  ```
  Save `bearer_token` (or `one_paste_mcp_url`) in local memory/scratch. When pairing multiple machines, save each machine's token separately so commands never cross rooms.
- **Web AI Connectors (Claude Web Custom Connectors / ChatGPT Developer Mode)**:
  - Add Remote MCP URL: `https://<your-tunnel>.trycloudflare.com/mcp`.
  - Complete the OAuth 2.0 popup by entering the 6-digit PIN.

### Step 4: Operate via the 5 Minimal MCP Tools & Binary File API
Send JSON-RPC `POST /mcp` with `Authorization: Bearer <token>`:
- `run_command`: Foreground command (`command`, `cwd`, `shell`, `timeout_sec`). Uses UTF-8 Base64 `-EncodedCommand` + async stream reading (`ReadToEndAsync()`) + silenced CLIXML progress noise on Windows.
- `start_command` / `read_command` / `stop_command`: Background process lifecycle (up to 4 concurrent jobs, clean `taskkill /T /F` tree cleanup on stop or exit).
- `file_transfer_info`: Returns tokenized `GET` / `PUT` / `DELETE` endpoints for `/files/<token>/?path=...` (up to 64 MB per file, zero-BOM binary I/O).

### Step 5: Clean Teardown
When finished, ask the user to close (`×`) the target PC's terminal window (destroying the room & tokens immediately), then stop the local `hub_server.py` and `cloudflared` daemon processes.

---

## 3. Battle-Tested Windows Lab & VDI Guardrails

Always enforce these rules when operating a Windows lab / shadow-restored VDI PC through this bridge:

1. **Persist All Work on the Non-Shadow Drive (`D:\`)**:
   - Lab VDIs typically reset `C:\` and `Desktop` on reboot. Store working scripts, temp captures, databases, and deliverables under `D:\work\` (or the non-restored data volume).
2. **CLI-First, Zero Unnecessary Mouse Stealing**:
   - Windows shares a single cursor & focus across the interactive session. Run all compilation, file parsing, database DDL/queries (`mysql.exe`), and verification silently via CLI first (`0%` cursor impact). Only touch GUI windows for a brief final display or screenshot pulse.
3. **Fixed-Ratio Window Geometry Guard for GUI Apps**:
   - Before clicking inside Qt/Delphi or custom-drawn desktop apps (whose toolbars collapse responsively and appear as opaque panels in Windows UIA), always:
     1. Enable 1:1 DPI awareness (`SetProcessDpiAwareness(2)`) before importing GUI libs.
     2. Record and restore a canonical window rectangle strictly in the order **`restore() -> activate() -> moveTo(left, top) -> resizeTo(width, height)`** (`moveTo` strictly before `resizeTo` so screen edges never clamp dimensions).
     3. Collapse auxiliary sidebars first and click window-relative offsets `(w.left + dx, w.top + dy)`.
4. **Strict Encoding & SQL Safety**:
   - Keep `.bat` / `.ps1` scripts **100% pure ASCII + CRLF (`\r\n`)** to avoid Windows CP936 DBCS quote-swallowing crashes.
   - Upload `.py` / `.sql` via `PUT /files/` (native zero-BOM UTF-8) or `[System.IO.File]::WriteAllText(..., (New-Object System.Text.UTF8Encoding($false)))`. Never use `Set-Content -Encoding UTF8` (writes BOM).
   - When writing SQL containing `DELIMITER $$` or `@vars` in PowerShell, always use **single-quoted Here-Strings (`@' ... '@`)** so PowerShell does not expand `$$`, and invoke CLI redirects through `cmd /c '"...\mysql.exe" ... < "D:\work\script.sql"'`.
5. **Binary Image Safety (Never Print Base64 to Terminal)**:
   - **NEVER print raw Base64 image strings to `stdout`** (terminal echo/truncation corrupts multimodal vision payloads and triggers `HTTP 400`).
   - Save screenshots to an ASCII path (e.g., `D:\work\screen.png`), download over `GET /files/<token>/?path=D:/work/screen.png`, and verify PNG (`89 50 4E 47`) or JPEG (`FF D8..FF D9`) headers + full PIL `Image.load()` before feeding to a vision model.
   - *Tip for non-ASCII filenames*: Prefer ASCII filenames for `GET /files/`, or pass `X-Target-Path-B64` / inspect content directly via `run_command` to avoid `latin-1` HTTP header encoding errors on `Content-Disposition`.
6. **Avoid Safety-Filter False Positives**:
   - Do not embed inline C# `DllImport` blocks, raw Base64 decoders, or credential dumps inside PowerShell command strings. Upload clean, readable Python/SQL files via `PUT /files/` and execute them normally.
