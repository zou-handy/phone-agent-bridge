# Windows VDI & Restricted Lab PC Automation Pitfalls (`WINDOWS_VDI_PITFALLS.md`)

> Real-world engineering notes distilled from operating AI Agents against shadow-restored Windows Cloud Desktops (VDI), campus/corporate NAT firewalls, nested VMware labs, and custom-drawn desktop apps — with zero installation and zero credential residue on the target PC.

---

## 1. Network, Protocol & Transport Pitfalls

### Pitfall 1: HTTP/1.1 Keep-Alive Unread Body Desync (`{}POST /limb/reply` -> `501`)
- **Symptom**: Local automated E2E tests pass 100%, yet as soon as the Windows limb connects through a real Cloudflare Tunnel (`:443`) + `.NET HttpWebRequest`, the server logs `{}POST /limb/reply` (returning `501 Unsupported method`) and command replies time out.
- **Root Cause**:
  - Python's `urllib.request` sends `Connection: close` per request, so leaving a 2-byte `{}` request body unread on `POST /limb/poll` goes unnoticed in short-lived local tests.
  - Under `HTTP/1.1` Keep-Alive (`.NET HttpWebRequest` `KeepAlive = true` + Cloudflare Tunnel connection pooling), those 2 unread bytes `{}` stay inside `self.rfile` and get prepended directly to the next request line on the reused TCP socket (`{}POST /limb/reply HTTP/1.1`).
- **Fix**:
  - Always drain small unread `Content-Length` bodies before sending an `HTTP/1.1` response (reset a `_body_drained` flag in `parse_request()`, explicitly call `_read_body()` in `/limb/poll` & `/limb/disconnect`, and guard `_send_bytes()` to drain `<= 64 KB` or set `close_connection = True` on oversized rejected payloads).

### Pitfall 2: Windows Defender Firewall High-IL Popups & Campus VLAN Isolation
- **Symptom**: Starting an inbound HTTP/SSH listener on `0.0.0.0:<port>` on a lab PC pops a Windows Defender Firewall dialog (`ShowNotificationDialog`) that Medium-IL automation scripts cannot click, while campus Wi-Fi and wired lab subnets sit behind cross-VLAN ACL isolation so direct LAN IP connections time out.
- **Root Cause**:
  - Inbound listeners on non-loopback interfaces trigger a High-IL (`S-1-16-12288`) firewall dialog protected by kernel **UIPI (User Interface Privilege Isolation)** — `win32kfull.sys` silently drops `SendInput` / `WM_COMMAND` from Medium-IL processes.
  - Campus/corporate switches commonly block wireless-to-wired-VLAN traffic and strip HTTP `Upgrade: websocket` headers on transparent proxies.
- **Fix**:
  - Never bind `0.0.0.0` on the target PC. Use strictly **outbound HTTPS (`:443`) HTTP/1.1 long-polling** (`threading.Condition` wakeups on the Hub) for wireless zero-cable bridges, or **`127.0.0.1` loopback + `adb reverse`** when a USB cable is available. Zero firewall prompts, zero UAC, penetrates NAT & transparent proxies cleanly.

### Pitfall 3: ConPTY Echo & Base64 Truncation -> Multimodal `HTTP 400` Session Deadlock
- **Symptom**: Printing a screenshot's Base64 string to an interactive terminal (`stdout`) or relaying large `.b64` blobs over rate-limited/1 MB-truncated text snippets corrupts a few bytes; feeding the resulting broken image to a vision model triggers `INVALID_ARGUMENT (HTTP 400)` and permanently breaks the conversation.
- **Root Cause**: Interactive terminals (Windows ConPTY / PTY) echo command input and wrap long lines, mixing shell text into the Base64 stream.
- **Fix**:
  - **Never print raw image Base64 to terminal `stdout`.**
  - Save captures as files on disk (prefer ASCII filenames, e.g., `D:\work\screen.png`), download over a dedicated binary HTTP channel (`GET /files/<token>/?path=...`), and assert file magic bytes (`\x89PNG\r\n\x1a\n` or `\xff\xd8..\xff\xd9`) plus a full pixel decode (`PIL.Image.open(...).load()`) before passing the image to a vision model.

---

## 2. Windows PowerShell & Encoding Pitfalls

### Pitfall 4: Windows CP936 DBCS Swallow-Quote Crash on Double-Click
- **Symptom**: A UTF-8 (no BOM) `.bat` or `.ps1` file containing CJK characters crashes instantly on double-click with unexplained syntax/unterminated-quote errors on Chinese Windows.
- **Root Cause**: Legacy `cmd.exe` and Windows PowerShell 5.1 default to CP936 (GBK). Trailing bytes of 3-byte UTF-8 CJK characters get misparsed as GBK lead bytes and swallow the immediately following `"` or `\r\n`.
- **Fix**: Keep all bootstrap `.bat` and `.ps1` scripts **100% pure ASCII + CRLF (`\r\n`)**.

### Pitfall 5: `.NET` 4KB Synchronous Pipe Deadlock & PowerShell 5.1 CLIXML Noise
- **Symptom**: Subprocesses producing `> 4 KB` of output hang forever until timeout; PowerShell 5.1 `-EncodedCommand` prints `#< CLIXML ...` progress XML into `stderr` even on clean `exit_code = 0` commands.
- **Root Cause**: Calling `WaitForExit()` before draining redirected `StandardOutput` / `StandardError` deadlocks once the 4 KB OS pipe buffer fills.
- **Fix**:
  - Pass commands as UTF-16LE Base64 via `-EncodedCommand` (immune to quote/newline escaping).
  - Always read both streams asynchronously with `$proc.StandardOutput.ReadToEndAsync()` and `$proc.StandardError.ReadToEndAsync()` before `WaitForExit()`.
  - Prepend `$ProgressPreference = 'SilentlyContinue';` inside the runner to keep `stderr` clean.

### Pitfall 6: `Set-Content -Encoding UTF8` BOM & PowerShell `$$` Expansion
- **Symptom**: Python scripts written via `Set-Content -Encoding UTF8` fail with `SyntaxError` on `\ufeff` BOM; SQL scripts containing `DELIMITER $$` written inside double-quoted Here-Strings (`@" ... "@`) fail with `DELIMITER cannot contain a backslash character`.
- **Root Cause**: In Windows PowerShell 5.1, `Set-Content -Encoding UTF8` writes a UTF-8 BOM (`\xEF\xBB\xBF`), and `$$` inside double quotes expands to the last token of the previous command (often a Windows path containing `\`).
- **Fix**:
  - Upload `.py` / `.sql` via binary `PUT /files/` or write explicitly with `[System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))`.
  - Always use **single-quoted Here-Strings (`@' ... '@`)** whenever SQL/code contains `$$` or `@vars`.
  - Execute `.sql` file redirection via `cmd /c '"...\mysql.exe" ... < "D:\work\script.sql"'` (PowerShell 5.1 does not support bare `<` stdin redirection).

---

## 3. Shadow-Restored VDI & Nested VMware Pitfalls

### Pitfall 7: Shadow-Protected `C:\` Wipes
- **Fact**: Cloud Desktop VDIs mount `C:\` (including `Desktop` and `AppData`) under a reboot-reset shadow driver. Only secondary data volumes (typically `D:\`) persist across power cycles.
- **Rule**: Place all working directories, scripts, SQL backups, and VM folders strictly on `D:\`.

### Pitfall 8: Nested VMware Workstation in GPU-less VDIs
When running a Linux VM inside a VDI session (nested virtualization), five recurring `.vmx` / boot traps hit immediately:
1. **Pure Black Screen**: VDI lacks a physical DX11/Vulkan GPU; VMware MKS 3D acceleration renders black.
   - Fix: `mks.enable3d = "FALSE"`
2. **Guest NIC Lost / No DHCP IP**: Selecting **"I copied it"** after moving a VM to `D:\` regenerates UUID/MAC and breaks Linux `netplan`/`udev` NIC bindings.
   - Fix: Always select **"I moved it"**, and pin `uuid.action = "keep"` + `ethernet0.checkMACAddress = "FALSE"`.
3. **Stuck at `720x400` Blinking Cursor (`_`)**: Disk has a GPT protective MBR (`0xEE`) installed under UEFI, but `.vmx` defaulted to Legacy BIOS.
   - Fix: `firmware = "efi"`
4. **Infinite GRUB Wait (`recordfail=1`)**: Forced power-off at lab shutdown sets GRUB timeout to `-1`.
   - Fix: Send an `Enter` keystroke (or disable `recordfail` pause inside the guest) to proceed.
5. **AHCI Stalls (`02 3a 00 Medium Not Present`)**: `.vmx` still references a deleted `.iso` on the restored `C:\` drive.
   - Fix: `sata0:1.present = "FALSE"` and `sata0:1.startConnected = "FALSE"`.

---

## 4. Windows Desktop Automation: Virtual Displays vs Fixed-Ratio Window Guard

### Pitfall 9: Why Hidden `CreateDesktop` Fails & "Two Kinds of Hands"
- **Why `CreateDesktop` / `SwitchDesktop` fails for live windows**: Windows NT forbids moving an already-created `HWND` across `Desktop` objects (`SetThreadDesktop` returns `ERROR_BUSY`), and non-active desktops lose physical input streams (`SendInput` / `pyautogui`).
- **Virtual Display Driver (VDD) boundary**: Great on personal machines with admin rights (`MoveWindow` slides windows onto/off the primary monitor in `<10ms`), **but unavailable on non-admin shadow-restored lab VDIs** — and even with VDD, the entire interactive session still shares **one single cursor & focus**.
- **Two Kinds of Hands**:
  1. **Cursor-Free Semantic Hands** (Browser **CDP**, WPF/WinUI **UIA `InvokePattern`**): never steal the user's mouse.
  2. **Physical Injection Hands** (`SendInput`, `pyautogui.click()`): always move the shared cursor.
- **Optimal Lab VDI Pattern**: Execute all real computation, compilation, and database DDL/testing silently via **CLI** (`0%` cursor interruption), reserving GUI injection strictly for a `<1s` final display pulse when an interactive desktop app must show live output.

### Pitfall 10: Fixed-Ratio Window Geometry Guard for UIA Black-Box Apps
- **Symptom**: Many veteran Windows desktop apps (Qt, Delphi, custom Direct2D — e.g., database IDEs) expose their toolbars to Windows UI Automation as one opaque container with zero child button nodes. Meanwhile, manually dragging or resizing the window triggers responsive toolbar collapse and breaks absolute pixel coordinates.
- **Battle-Tested Guard Protocol**:
  1. Call `SetProcessDpiAwareness(2)` **before** importing `pyautogui` / `ImageGrab` for exact 1:1 physical coordinates.
  2. Persist a canonical window bounding box `(left, top, width, height)` (for example, right-half split leaving the left half clear) and store clickable targets as **window-relative offsets `(dx, dy)`**.
  3. Immediately before any click sequence, restore the window strictly in this order:
     ```python
     if w.isMinimized or w.isMaximized:
         w.restore()       # 1. Clear maximize/minimize lock first
     w.activate()          # 2. Bring focus to foreground
     w.moveTo(left, top)   # 3. Move BEFORE resize so monitor edges never clamp width/height
     w.resizeTo(width, height)  # 4. Lock exact canonical dimensions
     ```
  4. Collapse auxiliary right-hand snippet/tag panels before running output-heavy views so multi-column tables are never clipped.

---

## 5. Quick Reference: MySQL 8.0 Lab Traps

- **`ERROR 1055` (`ONLY_FULL_GROUP_BY`)**: Prepend `SET sql_mode=(SELECT REPLACE(@@sql_mode, 'ONLY_FULL_GROUP_BY', ''));` when running legacy `GROUP BY` queries.
- **`ERROR 1418` (`CREATE FUNCTION` blocked by binlog)**: Execute `SET GLOBAL log_bin_trust_function_creators = 1;` and/or declare `DETERMINISTIC` on stored functions.
- **Parameter-Column Name Shadowing**: If a procedure/function parameter shares the exact name of a table column, qualify the column with the table name (`WHERE tbl.col = col`) to prevent a self-comparison tautology returning every row.
- **Function Syntax vs Procedure Syntax**: Never put `IN` / `OUT` keywords in `CREATE FUNCTION (...)` parameter lists.
- **Idempotent Full-Buffer Runs**: Prepend `USE <db>; DROP FUNCTION/PROCEDURE IF EXISTS <name>;` and execute the full editor buffer (`Ctrl+R`) instead of relying on fragile partial line selection (`Shift+Home`).
