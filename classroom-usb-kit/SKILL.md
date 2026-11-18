---
name: classroom-pc-usb
description: Zero-login USB cable takeover of a Windows classroom/lab PC (including Seewo Cloud Desktop VDI & nested VMware) from any mobile AI Agent container (Operit, DSH-Go, Termux Claude Code, Antigravity) using pc-run over adb reverse tcp:19999. Trigger whenever the user wants to run commands, edit code, query databases, or transfer files/screenshots on a USB-connected Windows PC without logging personal accounts onto the PC.
---

# Classroom PC USB Zero-Login Takeover (`classroom-pc-usb`)

This skill equips any **Mobile AI Agent** running on an Android phone (**Operit PRoot**, **DSH-Go (`@deepseek-ai/dsh`)**, **Termux + Claude Code / Gemini CLI**, or **Antigravity CLI**) to treat a public classroom/lab Windows PC as a disposable, high-speed execution limb over a single USB charging cable — **with zero account login, zero API keys, and zero credential residue on the PC**.

---

## 1. Architecture ("Brain on Phone, Ephemeral Limb on PC")

- **Phone Side (100% Private Brain)**:
  - Runs the LLM Agent, long-term memory, OAuth tokens, and `phone-side/pc-run` (`/usr/local/bin/pc-run` or `$PREFIX/bin/pc-run`).
  - Sends UTF-8 Base64-armored commands or binary file streams to `http://127.0.0.1:19999`.
- **USB Transport (`adb reverse` + Optional `adb forward`)**:
  - `adb reverse tcp:19999 tcp:19999`: tunnels phone `127.0.0.1:19999` -> PC `127.0.0.1:19999` over USB (`30~200 MB/s`, zero campus Wi-Fi/VLAN dependency).
  - `adb forward tcp:8765 tcp:8765` *(optional stateless WebUI projection)*: lets you open `http://127.0.0.1:8765/` in the classroom PC's browser to view/drive your phone agent's local WebUI on the big monitor. Because all state lives inside the phone, unplugging the cable leaves **zero cookies, tokens, or chat history** on the lab PC.
- **PC Side (Zero-Install Built-in `.NET HttpListener`)**:
  - `1_开启电脑被控桥(插线双击即可).bat` + `bridge_server.ps1` listen strictly on `127.0.0.1:19999` (loopback only — zero Windows Firewall UAC prompt, invisible to adjacent seats on the lab VLAN).
  - Includes a **2-second hot-plug watchdog** (`Start-Job`) so unplugging the phone during breaks automatically restores the tunnel within 2 seconds of re-plugging.

---

## 2. One-Time Setup for Mobile AI Agents

### A. Install `pc-run` & `SKILL.md` Inside Your Phone Container
- **Operit (PRoot Ubuntu) / Linux chroot / Antigravity**:
  ```bash
  cp classroom-usb-kit/phone-side/pc-run /usr/local/bin/pc-run
  chmod +x /usr/local/bin/pc-run
  ```
- **Termux (Claude Code / DSH-Go / Gemini CLI)**:
  ```bash
  cp classroom-usb-kit/phone-side/pc-run "$PREFIX/bin/pc-run"
  chmod +x "$PREFIX/bin/pc-run"
  ```
- **Mount Skill Prompt**:
  - **Claude Code / Gemini CLI / Antigravity**: copy `SKILL.md` to `~/.claude/skills/classroom-pc-usb/SKILL.md` or `~/.gemini/skills/classroom-pc-usb/SKILL.md`.
  - **Operit / DSH-Go**: import `SKILL.md` into your custom skill/prompt folder so the agent automatically invokes `pc-run` whenever PC tasks are mentioned.

### B. Stage the Offline Windows Kit on Phone MTP Storage (`/sdcard/`)
Choose either option so the kit is always sitting in your pocket when you walk into a lab:
1. **Option 1 — Out-of-the-Box Release Zip (`< 5 MB`, Recommended)**:
   Download **`Classroom_USB_Kit_v1.0.0.zip`** from [`zou-handy/phone-agent-bridge` Releases](https://github.com/zou-handy/phone-agent-bridge/releases/latest) and extract to `/sdcard/Classroom_USB_Kit/` (already bundles Google's official Windows `adb.exe`, `AdbWinApi.dll`, and `AdbWinUsbApi.dll`).
2. **Option 2 — Assemble from Source Repo**:
   Copy `classroom-usb-kit/` to `/sdcard/Classroom_USB_Kit/`, then download [Google's official Android SDK Platform-Tools for Windows](https://developer.android.com/tools/releases/platform-tools) and place `adb.exe`, `AdbWinApi.dll`, and `AdbWinUsbApi.dll` next to `bridge_server.ps1`.

---

## 3. Standard Operating Procedure (SOP) for the Agent

### Step 1: Probe the USB Bridge (`< 2s`)
```bash
pc-run --ping
```
- **If `OK: pong from Lab-PC`**: Proceed immediately with the user's task via `pc-run`.
- **If `Connection refused`**: Give the user these 3 concise steps:
  1. Plug the phone into the classroom PC via USB, select **File Transfer (MTP)**, and ensure **USB Debugging** is on (on Seewo Cloud Desktop VDI, check the phone USB device in the top USB toolbar).
  2. Copy `Classroom_USB_Kit` from phone internal storage to `D:\Classroom_USB_Kit\` on the PC (`D:\` survives `C:\` reboot shadow-wipes).
  3. Double-click `D:\Classroom_USB_Kit\1_开启电脑被控桥(插线双击即可).bat` (and tap **Allow USB Debugging** on the phone if prompted).

### Step 2: Execute Commands & Transfer Files Cleanly
- **Run PowerShell commands (defaults to `D:\` if present)**:
  ```bash
  pc-run "Get-ChildItem D:\Workspace | Select-Object Name, Mode, Length"
  ```
- **Open a project in the lab PC's VS Code**:
  ```bash
  pc-run "code D:\Workspace\LabProject"
  ```
- **Push `.py` / `.sql` scripts with zero UTF-8 BOM**:
  ```bash
  pc-run --push /tmp/solution.sql "D:\Workspace\solution.sql"
  ```
- **Pull screenshots / `.docx` deliverables with automatic JPEG/PNG magic-byte verification**:
  ```bash
  pc-run --pull "D:\Workspace\query_result.jpg" /tmp/query_result.jpg
  ```

---

## 4. Battle-Tested Classroom Iron Rules

1. **Pure ASCII `.bat` & `.ps1` + Zero UTF-8 BOM on Scripts**:
   - Keep `1_开启电脑被控桥(插线双击即可).bat`, `start_bridge.bat`, and `bridge_server.ps1` **100% pure ASCII with CRLF (`\r\n`)** — Windows CP936 `cmd.exe` / PowerShell 5.1 misinterprets trailing UTF-8 CJK bytes as DBCS lead bytes and swallows closing quotes.
   - Prefer `pc-run --push` or `[System.IO.File]::WriteAllText("D:\Workspace\script.py", $code, (New-Object System.Text.UTF8Encoding($false)))` instead of `Set-Content -Encoding UTF8` (which prepends a `\xEF\xBB\xBF` BOM).
   - For SQL files containing `DELIMITER $$` or `@vars`, either use `pc-run --push` or single-quoted PowerShell Here-Strings (`@' ... '@`) so PowerShell never expands `$$`.

2. **Never Print Image Base64 to Terminal `stdout` & Protect Phone Gallery**:
   - **NEVER print raw screenshot Base64 strings to `stdout`** (truncation or echo wrapping corrupts multimodal sessions with `HTTP 400 INVALID_ARGUMENT`). Always transfer binary files via `pc-run --pull` (which auto-checks JPEG `FF D8..FF D9` / PNG `89 50 4E 47` headers) or `adb.exe push`.
   - **Gallery Isolation**: Pull intermediate/diagnostic screenshots (`screen_check.jpg`) into container `/tmp/`, **never `/sdcard/`**, so Android `MediaStore` does not index diagnostic captures at the top of the user's photo album. Only copy final submission images to `/sdcard/DCIM/Camera/` when ready to submit.

3. **Windows 1:1 DPI Alignment & Seewo Cloud Desktop VDI Tips**:
   - Always call `ctypes.windll.shcore.SetProcessDpiAwareness(2)` **before** `import pyautogui` or `from PIL import ImageGrab` so coordinates match physical `1920x1080` 1:1.
   - On Seewo VDI nested VMware Workstation, ensure `.vmx` includes `mks.enable3d = "FALSE"` (fixes black screen without physical GPU), `uuid.action = "keep"`, and `ethernet0.checkMACAddress = "FALSE"` (prevents Linux NIC MAC mismatch when VMs live on `D:\`).
   - For MySQL 8.0 textbook `GROUP BY` exercises & stored functions, prepend `SET sql_mode=(SELECT REPLACE(@@sql_mode, "ONLY_FULL_GROUP_BY", ""));` and `SET GLOBAL log_bin_trust_function_creators = 1;` as needed.
