---
name: classroom-portal-mcp
description: Zero-cable, zero-login HTTPS :443 remote control of public, classroom, or borrowed Windows/Linux/macOS computers using a 5-tool Streamable HTTP MCP hub + OAuth 2.0 + 6-digit one-time screen PIN (hub_server.py + zero-download localhost.run SSH tunnel, cloudflared Quick Tunnel, or Cloudflare Workers). Trigger whenever the user wants an AI Agent (Doubao Work / Cloud PC, WorkBuddy, Antigravity, Claude Code, Codex, Cursor, OpenClaw, DSH-Go, or Web AI connectors) to run commands, edit code, query databases, transfer binary files, or drive GUI apps on a restricted/shadow-restored PC without plugging in a USB cable or logging personal accounts onto the PC.
---

# Classroom Portal MCP — Zero-Cable, Zero-Residue Lab PC Bridge (`classroom-portal-mcp`)

This skill equips any **Cloud Sandbox, Mobile, Desktop, CLI, or Web AI Agent** (**手机豆包「工作」模式 / 豆包云电脑**, **WorkBuddy**, **Antigravity**, **Claude Code**, **Codex**, **Cursor**, **OpenClaw**, **DSH-Go**, **Claude Web Custom Connectors**, **ChatGPT Developer Mode**) to connect to a restricted classroom or public computer over standard outbound **HTTPS (`:443`)** — **with zero USB cables, zero software installation, zero Windows Firewall popups, and zero account/API-key residue on the target computer**.

> **Deep Engineering Reference**: For root-cause postmortems of every Windows VDI, HTTP/1.1 Keep-Alive, PowerShell encoding, nested VMware, and fixed-ratio GUI automation pitfall behind this skill, read [`WINDOWS_VDI_PITFALLS.md`](./WINDOWS_VDI_PITFALLS.md).

---

## 1. Architecture & Security Boundaries

1. **Strict One-Way Control, Two-Way Data**:
   - **Control is strictly one-way (`Agent -> Target PC`)**: The disposable limb (`classroom_limb.ps1` / `limb_posix.py`) only long-polls `/limb/poll` for tasks and posts results to `/limb/reply`. The target PC has **zero access** to your personal machine, phone files, memory vault, or API keys.
   - **Data & Files flow both ways**: Execute commands (`run_command`, `start_command`, `read_command`, `stop_command`) and move binary/text files losslessly (`GET` / `PUT` / `DELETE /files/<token>/?path=...`).
2. **Multi-Room Isolation, Dynamic Tunnel Rotation & Instant Self-Destruct**:
   - One Hub supports **multiple target PCs simultaneously** (`activeRooms >= 1`). Each machine running the one-liner receives an isolated `room_id`, a unique 6-digit one-time PIN (immediately rotated upon successful pairing; auto-rotated after 5 failed attempts), and scoped bearer tokens.
   - `hub_server.py` defaults to `--public-url auto`, dynamically inferring the public HTTPS base URL from incoming `X-Forwarded-Host` / `Host` headers — so you can start `hub_server.py` **before** opening any tunnel, and ephemeral tunnel domain rotations (`localhost.run` / `pinggy`) never break `/go` or require restarting the Hub.
   - Closing the target PC's terminal window sends `/limb/disconnect` (or expires after `120s` idle TTL), immediately wiping the room and invalidating all tokens (`HTTP 401`).
3. **Zero-Domain Decentralized Rule (Essential When Sharing With Peers)**:
   - **Default & Recommended (`$0`, No Domain Required)**: Each user runs their own `hub_server.py` + ephemeral `:443` tunnel (`localhost.run` SSH tunnel or `cloudflared` Quick Tunnel). Every user gets an independent `:443` URL — zero domain cost, zero shared server maintenance, and zero cross-user correlation.
   - **Personal Custom Domain (`cloudflare/worker.js`)**: Reserve custom domains strictly for **single-user personal convenience** (fixed ultra-short command `irm https://your-domain/go | iex`, 24/7 serverless).

---

## 2. Standard Operating Procedure (SOP) for the Agent

### Step 1: Start Local Hub First, Then Open a `:443` Tunnel

First, start `hub_server.py` in the background (no `--public-url` required; writes PID to `/tmp/classroom_portal_hub.pid`):

```bash
python3 classroom-portal-mcp/hub_server.py --host 127.0.0.1 --port 8787 \
  --pidfile /tmp/classroom_portal_hub.pid > /tmp/portal_hub.log 2>&1 &
```
*(If port `8787` is busy, run `python3 classroom-portal-mcp/hub_server.py --stop` first or pass `--port 8788`.)*

Next, open an outbound `:443` HTTPS tunnel using **Route A** (best for Cloud AI Sandboxes / Datacenter IPs) or **Route B** (best for Local PCs / Residential / Campus IPs):

- **Route A — Zero-Download SSH Tunnel via `localhost.run` (Recommended inside 豆包云电脑 / Cloud Sandboxes / Datacenter ASNs where `trycloudflare` returns `503`)**:
  ```bash
  rm -f /tmp/portal_tunnel.log
  ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ServerAliveInterval=30 \
    -R 80:127.0.0.1:8787 nokey@localhost.run > /tmp/portal_tunnel.log 2>&1 < /dev/null &
  echo $! > /tmp/classroom_portal_tunnel.pid
  URL=""
  for _ in $(seq 1 30); do
    URL=$(grep -oE 'https://[a-zA-Z0-9.-]+\.lhr\.life' /tmp/portal_tunnel.log 2>/dev/null | head -n 1 || true)
    [ -n "$URL" ] && break
    sleep 0.5
  done
  echo "TUNNEL_URL=$URL"
  curl -fsSL "$URL/health"
  ```

- **Route B — `cloudflared` Quick Tunnel (Recommended on WorkBuddy Local PC / Mac / Residential Networks)**:
  1. If `cloudflared` is not installed on Linux x86_64/arm64, download the official binary (**use `https://ghfast.top/` mirror prefix automatically if direct GitHub Release download stalls >8s in Mainland China**):
     ```bash
     ARCH=$(uname -m); BIN=$([ "$ARCH" = "aarch64" ] && echo cloudflared-linux-arm64 || echo cloudflared-linux-amd64)
     GH_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/$BIN"
     curl -fL --connect-timeout 8 --max-time 25 "$GH_URL" -o /tmp/cloudflared \
       || curl -fL --connect-timeout 8 --max-time 60 "https://ghfast.top/$GH_URL" -o /tmp/cloudflared
     chmod +x /tmp/cloudflared
     ```
  2. Start Quick Tunnel and **loop-poll the log** (do NOT rely on a fixed `sleep 5`):
     ```bash
     rm -f /tmp/portal_tunnel.log
     /tmp/cloudflared tunnel --protocol http2 --edge-ip-version 4 --no-autoupdate \
       --url http://127.0.0.1:8787 > /tmp/portal_tunnel.log 2>&1 &
     echo $! > /tmp/classroom_portal_tunnel.pid
     URL=""
     for _ in $(seq 1 30); do
       URL=$(grep -oE 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' /tmp/portal_tunnel.log 2>/dev/null | head -n 1 || true)
       [ -n "$URL" ] && break
       sleep 0.5
     done
     echo "TUNNEL_URL=$URL"
     curl -fsSL "$URL/health"
     ```
  3. **Known Cloudflared Log Diagnostics**:
     - `ICMP proxy` / `ping_group_range` / `socket: permission denied` warnings in unprivileged containers are **100% harmless** — ignore them.
     - If the log prints `503 quick tunnel provisioning failed`, Cloudflare's Quick Tunnel API is rate-limiting your cloud datacenter ASN — **do NOT waste time tweaking flags; immediately switch to Route A (`localhost.run`) above**.

### Step 2: Bootstrap the Target Classroom PC (Pure RAM, Zero Install)
Give the user the generated one-liner for each target computer:

- **Windows Target PC (`Win + R` or PowerShell)**:
  ```powershell
  powershell -NoP -Ep Bypass -c "irm <TUNNEL_URL>/go | iex"
  ```
  *(Or double-click `limb\start_classroom_portal.bat` and paste `<TUNNEL_URL>`.)*
- **Linux / macOS Target PC**:
  ```bash
  curl -fsSL <TUNNEL_URL>/go.py | python3 -
  ```

Each target PC prints its own one-time **6-digit PIN** (e.g., `743-601`) and triggers **no Windows Defender Firewall or UAC popup**.

### Step 3: Pair With the 6-Digit PIN
Ask the user for the 6-digit PIN shown on the target screen (dashes like `743-601` are accepted):

- **CLI / Cloud Sandbox / Desktop Agent Direct Pairing**:
  ```bash
  curl -fsSL -X POST "http://127.0.0.1:8787/pair" \
    -H "Content-Type: application/json" \
    -d '{"pin":"743-601"}'
  ```
  Save `bearer_token` (or `one_paste_mcp_url`). When controlling multiple machines simultaneously, store each machine's `bearer_token` separately so commands never cross rooms.
- **Remote MCP Connectors (WorkBuddy / 豆包工作模式 MCP / Claude Web / ChatGPT Developer Mode)**:
  - Either paste the `one_paste_mcp_url` (`<TUNNEL_URL>/mcp/cp_at_...`) directly into your MCP client, or add `<TUNNEL_URL>/mcp` and enter the 6-digit PIN in the OAuth 2.0 popup.

### Step 4: Operate via the 5 Minimal MCP Tools & Binary File API
Send JSON-RPC `POST http://127.0.0.1:8787/mcp` (or `<TUNNEL_URL>/mcp`) with `Authorization: Bearer <token>`:
- `run_command`: Foreground command (`command`, `cwd`, `shell`, `timeout_sec`). Uses UTF-8 Base64 `-EncodedCommand` + async stream reading (`ReadToEndAsync()`) + silenced CLIXML progress noise on Windows.
- `start_command` / `read_command` / `stop_command`: Background process lifecycle (up to 4 concurrent jobs, clean `taskkill /T /F` tree cleanup on stop or exit).
- `file_transfer_info`: Returns tokenized `GET` / `PUT` / `DELETE` endpoints for `/files/<token>/?path=...` (up to 64 MB per file, zero-BOM binary I/O; pass `X-Target-Path-B64` when paths contain non-ASCII characters).

### Step 5: Safe Teardown (Never Use `pkill -f`)
When finished:
1. Ask the user to close (`×`) the target PC's terminal window (destroying the room & tokens immediately with `HTTP 401`).
2. Stop the local Hub and tunnel safely via PID files (**NEVER run bare `pkill -f cloudflared`, which matches its own shell command line and kills your subshell with `SIGTERM 143`**):
   ```bash
   python3 classroom-portal-mcp/hub_server.py --stop
   ```

---

## 3. Battle-Tested Windows Classroom & VDI Guardrails

Always enforce these rules when operating a Windows classroom / shadow-restored VDI PC through this bridge:

1. **Persist All Work on the Non-Shadow Drive (`D:\`)**:
   - Classroom VDIs typically reset `C:\` and `Desktop` on reboot. Store working scripts, temp captures, databases, and deliverables under `D:\work\` (when testing on a personal PC without `D:\`, inspect first and read-only unless asked).
2. **CLI-First, Zero Unnecessary Mouse Stealing**:
   - Windows shares a single cursor & focus across the interactive session. Run all compilation, file parsing, database DDL/queries (`mysql.exe`), and verification silently via CLI first (`0%` cursor impact). Only touch GUI windows for a brief final display or screenshot pulse.
3. **Fixed-Ratio Window Geometry Guard for GUI Apps (`Computer Use`)**:
   - Before clicking inside Qt/Delphi or custom-drawn desktop apps (whose toolbars collapse responsively and appear as opaque panels in Windows UIA), always:
     1. Enable 1:1 DPI awareness (`SetProcessDpiAwareness(2)`) before importing GUI libs.
     2. Record and restore a canonical window rectangle strictly in the order **`restore() -> activate() -> moveTo(left, top) -> resizeTo(width, height)`** (`moveTo` strictly before `resizeTo` so screen edges never clamp dimensions).
     3. Collapse auxiliary sidebars first and click window-relative offsets `(w.left + dx, w.top + dy)`.
4. **Windows Shell, Read/Write UTF-8 Encoding & Path Escaping Iron Rules**:
   - **Always Keep Default `powershell` on Windows**: NEVER pass `shell: "cmd"` to chain commands (`echo A; echo B` in `cmd.exe` outputs everything on one line without expanding subsequent variables). Use default `powershell` + `Write-Output`.
   - **Always Pass `-Encoding UTF8 -LiteralPath` When Reading Files in PowerShell 5.1**: `Get-Content`, `Select-String`, and `Import-Csv` in Windows PowerShell 5.1 default to ANSI (`CP936`), turning UTF-8 Chinese text into mojibake (`寮€鍚...`), and `[` / `(` in filenames can trigger wildcard errors without `-LiteralPath`. Always write:
     ```powershell
     Get-Content -Encoding UTF8 -LiteralPath 'C:\Users\Student\Desktop\notes.md'
     ```
   - **Caller-Side Backslash Safety (`\U` Trap)**: When calling `/mcp` from Bash or Python on Linux, NEVER inline raw Windows paths like `"C:\Users\..."` inside Python string literals (`\U` triggers `SyntaxError: unicodeescape`). Write JSON payloads via single-quoted heredocs (`cat << 'EOF' > /tmp/req.json`) or use forward slashes / `Path` objects.
   - **Zero-BOM Writes & SQL `$$` Safety**: Keep `.bat` / `.ps1` **100% pure ASCII + CRLF (`\r\n`)**. Upload `.py` / `.sql` via `PUT /files/` or `UTF8Encoding($false)` (never `Set-Content -Encoding UTF8`). Wrap SQL containing `DELIMITER $$` in single-quoted Here-Strings (`@' ... '@`).
5. **Binary Image Safety (Never Print Base64 to Terminal)**:
   - **NEVER print raw Base64 image strings to `stdout`** (terminal echo/truncation corrupts multimodal vision payloads and triggers `HTTP 400`). Save screenshots to `D:\work\screen.png` and download via `GET /files/<token>/?path=D:/work/screen.png`.
6. **Avoid Safety-Filter False Positives**:
   - Do not embed inline C# `DllImport` blocks, raw Base64 decoders, or credential dumps inside PowerShell command strings. Upload clean, readable Python/SQL files via `PUT /files/` and execute them normally.
