#!/usr/bin/env python3
"""
Classroom Portal MCP Hub (`hub_server.py`) — Zero-Dependency Python 3.12 Stdlib
Combines three designs into one cohesive zero-residue bridge:
  1) `s3hq4y/portal`: Minimal 5-tool Streamable HTTP MCP (`run_command`, `start_command`,
     `read_command`, `stop_command`, `file_transfer_info`) + `/files/<token>/...` HTTP binary API.
  2) `mrkaylabs/easy-mcp`: Full OAuth 2.0 Dynamic Client Registration (RFC 7591), Protected Resource
     Metadata (RFC 9728), Authorization Server Metadata (RFC 8414), and PKCE (`S256`) for Web AI
     connectors (Claude Web Custom Connectors, ChatGPT Developer Mode, Cursor, etc.).
  3) `#59` Classroom 6-Digit Pairing Code Gate: Fixes `easy-mcp`'s zero-auth RCE flaw by gating
     `/oauth/authorize` (and CLI `/pair`) behind the live 6-digit PIN shown on the classroom PC,
     with constant-time comparison, 5-strike PIN burn/rotation, and instant room wipe on window close.
"""
import argparse
import base64
import errno
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import signal
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SERVER_INFO = {"name": "classroom-portal-mcp", "version": "1.0.0"}
SUPPORTED_MCP_PROTOCOLS = ["2025-03-26", "2024-11-05"]
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
ROOM_IDLE_TTL_SEC = 120.0
POLL_WAIT_SEC = 25.0
MAX_PIN_STRIKES = 5
MAX_ACTIVE_ROOMS = 32
_VALID_HOST_RE = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*|\[[0-9a-fA-F:]+\])(?::\d{1,5})?$"
)
_TRUSTED_TUNNEL_SUFFIXES = (
    ".lhr.life",
    ".localhost.run",
    ".trycloudflare.com",
    ".pinggy.link",
    ".pinggy.io",
    ".workers.dev",
    ".ngrok-free.app",
    ".ngrok.io",
)

BASE_DIR = Path(__file__).resolve().parent
LIMB_PS1_PATH = BASE_DIR / "limb" / "classroom_limb.ps1"
LIMB_PY_PATH = BASE_DIR / "limb" / "limb_posix.py"

PORTAL_TOOLS = [
    {
        "name": "run_command",
        "description": (
            "Run a foreground shell command on the paired classroom computer (default timeout 120s, max 600s). "
            "Uses UTF-8 Base64 encoding and async stream reading on Windows to avoid CP936 quote issues and 4KB pipe deadlocks. "
            "IMPORTANT on Windows: ALWAYS keep default `powershell` (do NOT pass `shell: 'cmd'` to chain statements — cmd.exe treats `;` literally). "
            "When reading UTF-8 files in PowerShell 5.1, ALWAYS add `-Encoding UTF8 -LiteralPath` (e.g. `Get-Content -Encoding UTF8 -LiteralPath ...`)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command string to execute (on Windows use PowerShell + Write-Output; pass -Encoding UTF8 -LiteralPath to Get-Content)."},
                "cwd": {"type": "string", "description": "Working directory (defaults to D:\\ on Windows or workspace root)."},
                "shell": {
                    "type": "string",
                    "enum": ["powershell", "pwsh", "cmd", "bash", "sh"],
                    "description": "Shell to use (default: powershell on Windows, bash/sh on POSIX; avoid cmd for multi-statement commands).",
                },
                "timeout_sec": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 600,
                    "description": "Execution timeout in seconds (default: 120).",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    {
        "name": "start_command",
        "description": "Start a long-running background process on the paired classroom computer (max 4 concurrent jobs). Returns command_id and pid. On Windows keep default `powershell`.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Background command to launch."},
                "cwd": {"type": "string", "description": "Working directory."},
                "shell": {"type": "string", "enum": ["powershell", "pwsh", "cmd", "bash", "sh"]},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_command",
        "description": "Read incremental stdout/stderr from a background job by offset, or omit command_id to list all active jobs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command_id": {"type": "string", "description": "Job ID from start_command. Omit to list jobs."},
                "offset": {"type": "integer", "minimum": 0, "description": "Character offset to read from (default: 0)."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "stop_command",
        "description": "Stop a background job and its child process tree on the paired classroom computer.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command_id": {"type": "string", "description": "Job ID to terminate."},
                "force": {"type": "boolean", "description": "Immediate tree kill without grace period (default: true)."},
            },
            "required": ["command_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "file_transfer_info",
        "description": "Return tokenized HTTP file API endpoints (GET download / PUT zero-BOM upload / DELETE), limits, and curl examples for moving binary or text files cleanly outside JSON-RPC.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]

DEFAULT_INSTRUCTIONS = (
    "Connected to a zero-residue classroom computer via Classroom Portal MCP (5 minimal tools + HTTP file API).\n"
    "- Use `run_command` for foreground commands (on Windows keep default `powershell` + `Write-Output`; NEVER pass `shell: 'cmd'` for multi-line statements; ALWAYS pass `-Encoding UTF8 -LiteralPath` to `Get-Content` / `Select-String` / `Import-Csv`).\n"
    "- Use `start_command` / `read_command` / `stop_command` for background servers or watchers.\n"
    "- Use `file_transfer_info` to get the HTTP `/files/<token>/<path>` endpoints for binary uploads/downloads (.docx, .jpg, .zip).\n"
    "- All state lives strictly in RAM and self-destructs when the classroom window closes."
)


def _sha256_hex(val: str) -> str:
    return hashlib.sha256(val.encode("utf-8")).hexdigest()


def _safe_eq(a: str, b: str) -> bool:
    if not a or not b:
        return False
    ha = hashlib.sha256(a.encode("utf-8")).digest()
    hb = hashlib.sha256(b.encode("utf-8")).digest()
    return hmac.compare_digest(ha, hb)


def _norm_pin(raw: str) -> str:
    return "".join(ch for ch in str(raw or "") if ch.isdigit())


def _gen_6digit_pin(existing_pins: set[str]) -> str:
    for _ in range(100):
        pin = f"{secrets.randbelow(900000) + 100000:06d}"
        if pin not in existing_pins:
            return pin
    return f"{secrets.randbelow(900000) + 100000:06d}"


def _valid_redirect_uri(uri: str) -> bool:
    try:
        u = urllib.parse.urlparse(uri)
        if u.scheme == "https" and u.netloc:
            return True
        if u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "::1"):
            return True
        # Native desktop/mobile custom schemes (e.g. cursor://, claude://)
        if u.scheme and u.scheme not in ("javascript", "data", "file", "http") and ":" not in u.scheme:
            return True
    except Exception:
        pass
    return False


class HubState:
    """In-memory ephemeral state for rooms, OAuth clients, auth codes, and scoped tokens (0 disk residue)."""

    def __init__(self, public_base_url: str = "", allow_remote_pair: bool = False):
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        cleaned = (public_base_url or "").strip().rstrip("/")
        self.public_base_url = "" if cleaned.lower() == "auto" else cleaned
        self.allow_remote_pair = bool(allow_remote_pair)
        self.last_seen_public_url = ""
        self.csrf_key = secrets.token_hex(32)

        self.rooms: dict[str, dict] = {}          # room_id -> room dict
        self.pin_to_room: dict[str, str] = {}     # 6-digit pin -> room_id
        self.limb_secret_to_room: dict[str, str] = {}  # sha256(limb_secret) -> room_id

        self.oauth_clients: dict[str, dict] = {}  # client_id -> {secret_hash, redirect_uris, auth_method, created_at}
        self.oauth_codes: dict[str, dict] = {}    # code -> {room_id, client_id, redirect_uri, challenge, method, exp}
        self.tokens: dict[str, dict] = {}         # sha256(token) -> {type, room_id, client_id, exp, raw_file_tok}
        self.ip_failures: dict[str, list[float]] = {}  # ip -> timestamps of recent PIN failures

    @staticmethod
    def _is_local_host(host: str) -> bool:
        if not host:
            return True
        h = host.split("/")[0].strip().lower()
        if h.startswith("["):
            h_only = h.split("]")[0] + "]"
        else:
            h_only = h.split(":")[0]
        return h_only in ("127.0.0.1", "localhost", "[::1]", "::1", "0.0.0.0")

    @staticmethod
    def _sanitize_host(candidate: str) -> str:
        h = (candidate or "").split(",")[0].strip()
        if not h or len(h) > 253:
            return ""
        return h if _VALID_HOST_RE.fullmatch(h) else ""

    @staticmethod
    def _is_trusted_tunnel_host(host: str) -> bool:
        h_only = (host or "").split(":")[0].strip().lower()
        return any(h_only.endswith(sfx) for sfx in _TRUSTED_TUNNEL_SUFFIXES)

    def base_url_for(self, host_header: str, x_forwarded_host: str = "", x_forwarded_proto: str = "") -> str:
        xfh = self._sanitize_host(x_forwarded_host)
        hh = self._sanitize_host(host_header)
        raw_host = xfh or hh
        xfp = (x_forwarded_proto or "").split(",")[0].strip().lower()

        if raw_host and not self._is_local_host(raw_host):
            scheme = xfp if xfp in ("https", "http") else "https"
            live_url = f"{scheme}://{raw_host}"
            if self._is_trusted_tunnel_host(raw_host):
                with self.lock:
                    self.last_seen_public_url = live_url
            if not self.public_base_url:
                return live_url
            # Seamlessly follow rotating ephemeral tunnel hostnames (localhost.run / trycloudflare / pinggy)
            if any(ep in self.public_base_url.lower() for ep in (".lhr.life", ".localhost.run", ".trycloudflare.com", "pinggy")):
                return live_url
            return self.public_base_url

        if self.public_base_url:
            return self.public_base_url
        with self.lock:
            if self.last_seen_public_url:
                return self.last_seen_public_url
        host = raw_host or "127.0.0.1:8787"
        scheme = xfp if xfp in ("https", "http") else ("http" if self._is_local_host(host) else "https")
        return f"{scheme}://{host}"

    def make_csrf(self, bind_str: str) -> str:
        ts = str(int(time.time() // 600))
        sig = hmac.new(self.csrf_key.encode(), f"{ts}:{bind_str}".encode(), hashlib.sha256).hexdigest()[:24]
        return f"{ts}.{sig}"

    def verify_csrf(self, token: str, bind_str: str) -> bool:
        if not token or "." not in token:
            return False
        ts_str, sig = token.split(".", 1)
        try:
            ts = int(ts_str)
        except ValueError:
            return False
        now_bucket = int(time.time() // 600)
        if abs(now_bucket - ts) > 2:
            return False
        expected = hmac.new(self.csrf_key.encode(), f"{ts_str}:{bind_str}".encode(), hashlib.sha256).hexdigest()[:24]
        return hmac.compare_digest(sig, expected)

    def gc_locked(self):
        now = time.monotonic()
        now_wall = time.time()
        dead_rooms = [
            rid for rid, r in self.rooms.items()
            if (now - r["last_seen"]) > ROOM_IDLE_TTL_SEC
        ]
        for rid in dead_rooms:
            self._destroy_room_locked(rid)

        dead_codes = [c for c, v in self.oauth_codes.items() if v["exp"] < now_wall]
        for c in dead_codes:
            self.oauth_codes.pop(c, None)

        dead_toks = [
            th for th, v in self.tokens.items()
            if (v["exp"] and v["exp"] < now_wall) or v["room_id"] not in self.rooms
        ]
        for th in dead_toks:
            self.tokens.pop(th, None)

    def _destroy_room_locked(self, room_id: str):
        room = self.rooms.pop(room_id, None)
        if not room:
            return
        if room.get("pin"):
            self.pin_to_room.pop(room["pin"], None)
        if room.get("limb_secret_hash"):
            self.limb_secret_to_room.pop(room["limb_secret_hash"], None)
        for req_entry in room.get("inflight", {}).values():
            req_entry["error"] = "Classroom computer disconnected (window closed)."
            req_entry["done"] = True
        dead_toks = [th for th, v in self.tokens.items() if v["room_id"] == room_id]
        for th in dead_toks:
            self.tokens.pop(th, None)
        self.cond.notify_all()

    def register_limb(self, hostname: str, os_info: str, default_cwd: str) -> dict:
        with self.lock:
            self.gc_locked()
            if len(self.rooms) >= MAX_ACTIVE_ROOMS:
                unpaired = sorted(
                    (r for r in self.rooms.values() if r.get("paired_count", 0) == 0),
                    key=lambda r: r["created_at"],
                )
                if unpaired:
                    self._destroy_room_locked(unpaired[0]["room_id"])
                else:
                    raise OverflowError("Too many active paired rooms")
            room_id = "rm_" + secrets.token_hex(8)
            limb_secret = "ls_" + secrets.token_urlsafe(24)
            limb_hash = _sha256_hex(limb_secret)
            pin = _gen_6digit_pin(set(self.pin_to_room.keys()))
            room = {
                "room_id": room_id,
                "pin": pin,
                "pin_rotated_note": "",
                "strikes": 0,
                "paired_count": 0,
                "limb_secret_hash": limb_hash,
                "hostname": str(hostname or "Classroom-PC")[:64],
                "os_info": str(os_info or "Windows")[:64],
                "default_cwd": str(default_cwd or "D:\\")[:260],
                "created_at": time.time(),
                "last_seen": time.monotonic(),
                "queue": [],       # list of task dicts waiting for limb
                "inflight": {},    # task_id -> {"done": bool, "result": any, "error": str}
            }
            self.rooms[room_id] = room
            self.pin_to_room[pin] = room_id
            self.limb_secret_to_room[limb_hash] = room_id
            self.cond.notify_all()
            return {
                "room_id": room_id,
                "limb_secret": limb_secret,
                "pin": pin,
                "pin_formatted": f"{pin[:3]}-{pin[3:]}",
            }

    def rotate_room_pin_locked(self, room: dict, reason: str) -> str:
        old_pin = room.get("pin")
        if old_pin:
            self.pin_to_room.pop(old_pin, None)
        new_pin = _gen_6digit_pin(set(self.pin_to_room.keys()))
        room["pin"] = new_pin
        room["strikes"] = 0
        room["pin_rotated_note"] = reason
        self.pin_to_room[new_pin] = room["room_id"]
        self.cond.notify_all()
        return new_pin

    def verify_and_consume_pin(self, client_ip: str, raw_pin: str) -> tuple[bool, str, str]:
        """
        Verify 6-digit PIN in constant time with IP rate-limiting and 5-strike room PIN rotation.
        On success, rotates the room's PIN immediately so each 6-digit code is strictly one-time!
        Returns `(ok, room_id_or_empty, error_message)`.
        """
        pin = _norm_pin(raw_pin)
        now = time.monotonic()
        with self.lock:
            self.gc_locked()
            recent_fails = [t for t in self.ip_failures.get(client_ip, []) if now - t < 60.0]
            if len(recent_fails) >= 8:
                self.ip_failures[client_ip] = recent_fails
                return False, "", "尝试过于频繁，请等 60 秒后再试。"

            if len(pin) != 6:
                recent_fails.append(now)
                self.ip_failures[client_ip] = recent_fails
                return False, "", "请输入完整的 6 位数字配对码。"

            matched_rid = None
            for cand_pin, rid in list(self.pin_to_room.items()):
                if _safe_eq(pin, cand_pin):
                    matched_rid = rid

            if not matched_rid or matched_rid not in self.rooms:
                recent_fails.append(now)
                self.ip_failures[client_ip] = recent_fails
                # Increment strike on active rooms if single/few rooms exist to prevent blind spray
                for r in list(self.rooms.values()):
                    r["strikes"] += 1
                    if r["strikes"] >= MAX_PIN_STRIKES:
                        self.rotate_room_pin_locked(r, "5 failed PIN attempts — auto-rotated for safety")
                return False, "", "配对码无效或已过期，请核对机房电脑屏幕上的最新 6 位数字。"

            room = self.rooms[matched_rid]
            room["paired_count"] += 1
            self.rotate_room_pin_locked(room, "Paired new AI client — rotated one-time PIN")
            return True, matched_rid, ""

    def mint_tokens_for_room(self, room_id: str, client_id: str, keep_refresh: str = "") -> dict:
        with self.lock:
            access_tok = "cp_at_" + secrets.token_urlsafe(32)
            now_wall = time.time()
            self.tokens[_sha256_hex(access_tok)] = {
                "type": "access",
                "room_id": room_id,
                "client_id": client_id,
                "exp": now_wall + 43200,  # 12h max (or until classroom window closes)
                "raw_tok": access_tok,
            }
            refresh_tok = keep_refresh
            if not refresh_tok:
                refresh_tok = "cp_rt_" + secrets.token_urlsafe(32)
                self.tokens[_sha256_hex(refresh_tok)] = {
                    "type": "refresh",
                    "room_id": room_id,
                    "client_id": client_id,
                    "exp": now_wall + 86400,
                    "raw_tok": "",
                }
            return {
                "access_token": access_tok,
                "token_type": "Bearer",
                "expires_in": 43200,
                "refresh_token": refresh_tok,
                "scope": "mcp",
            }

    def authenticate_mcp_token(self, raw_tok: str) -> tuple[dict | None, str]:
        if not raw_tok:
            return None, ""
        with self.lock:
            self.gc_locked()
            rec = self.tokens.get(_sha256_hex(raw_tok))
            if not rec or rec["type"] != "access":
                return None, ""
            room = self.rooms.get(rec["room_id"])
            if not room:
                return None, ""
            return room, raw_tok

    def dispatch_to_limb(self, room_id: str, op: str, payload: dict, timeout_sec: float = 130.0) -> dict:
        task_id = "tk_" + secrets.token_hex(8)
        entry = {"done": False, "result": None, "error": ""}
        with self.cond:
            room = self.rooms.get(room_id)
            if not room:
                raise RuntimeError("机房受控端已断开或窗口已关闭。")
            room["inflight"][task_id] = entry
            room["queue"].append({"task_id": task_id, "op": op, "payload": payload})
            self.cond.notify_all()

            deadline = time.monotonic() + timeout_sec
            while not entry["done"]:
                rem = deadline - time.monotonic()
                if rem <= 0:
                    room["inflight"].pop(task_id, None)
                    raise TimeoutError(f"等待机房电脑响应超时 ({int(timeout_sec)}s)")
                self.cond.wait(timeout=min(rem, 2.0))
                if room_id not in self.rooms:
                    raise RuntimeError("机房受控端在执行期间断开。")

            room = self.rooms.get(room_id)
            if room:
                room["inflight"].pop(task_id, None)
            if entry["error"]:
                raise RuntimeError(entry["error"])
            return entry["result"] if isinstance(entry["result"], dict) else {"ok": False, "error": "Invalid limb response"}

    def limb_poll(self, limb_secret: str) -> dict:
        lhash = _sha256_hex(limb_secret)
        with self.cond:
            rid = self.limb_secret_to_room.get(lhash)
            room = self.rooms.get(rid) if rid else None
            if not room:
                return {"ok": False, "error": "room_not_found"}
            room["last_seen"] = time.monotonic()

            deadline = time.monotonic() + POLL_WAIT_SEC
            while not room["queue"] and not room["pin_rotated_note"]:
                rem = deadline - time.monotonic()
                if rem <= 0:
                    break
                self.cond.wait(timeout=rem)
                if rid not in self.rooms:
                    return {"ok": False, "error": "room_closed"}
                room["last_seen"] = time.monotonic()

            note = room["pin_rotated_note"]
            room["pin_rotated_note"] = ""
            tasks = room["queue"][:]
            room["queue"].clear()
            pin = room["pin"]
            return {
                "ok": True,
                "pin": pin,
                "pin_formatted": f"{pin[:3]}-{pin[3:]}",
                "pin_note": note,
                "paired_count": room["paired_count"],
                "tasks": tasks,
            }

    def limb_reply(self, limb_secret: str, task_id: str, result: dict) -> bool:
        lhash = _sha256_hex(limb_secret)
        with self.cond:
            rid = self.limb_secret_to_room.get(lhash)
            room = self.rooms.get(rid) if rid else None
            if not room:
                return False
            room["last_seen"] = time.monotonic()
            entry = room["inflight"].get(task_id)
            if entry is not None:
                entry["result"] = result
                entry["done"] = True
                self.cond.notify_all()
            return True

    def limb_disconnect(self, limb_secret: str) -> bool:
        lhash = _sha256_hex(limb_secret)
        with self.cond:
            rid = self.limb_secret_to_room.get(lhash)
            if not rid:
                return False
            self._destroy_room_locked(rid)
            return True


def _render_authorize_html(
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    code_challenge_method: str,
    scope: str,
    csrf_token: str,
    error_msg: str = "",
) -> str:
    err_block = (
        f'<div class="err">{html.escape(error_msg)}</div>'
        if error_msg
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>机房 6 位配对码授权 · Classroom Portal MCP</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
          background:#131314; color:#e3e3e3; font-family:system-ui,-apple-system,sans-serif; padding:16px; box-sizing:border-box; }}
  .card {{ width:100%; max-width:380px; background:#1e1f20; border:1px solid #333537; border-radius:16px; padding:24px; box-sizing:border-box; }}
  h1 {{ font-size:18px; margin:0 0 8px; color:#fff; }}
  p {{ font-size:13px; color:#a8c7fa; line-height:1.5; margin:0 0 18px; }}
  .meta {{ font-size:12px; color:#8e918f; margin-bottom:16px; word-break:break-all; }}
  .err {{ background:#3c1f1f; border:1px solid #f28b82; color:#f28b82; padding:10px 12px; border-radius:10px; font-size:13px; margin-bottom:14px; }}
  input[type="text"] {{ width:100%; box-sizing:border-box; font-size:26px; letter-spacing:6px; text-align:center;
                        padding:12px; border-radius:12px; border:1px solid #444746; background:#131314; color:#fff; outline:none; font-family:ui-monospace,monospace; }}
  input[type="text"]:focus {{ border-color:#a8c7fa; }}
  button {{ width:100%; margin-top:16px; padding:12px; border-radius:12px; border:0; background:#a8c7fa; color:#062e6f;
            font-size:15px; font-weight:600; cursor:pointer; }}
  .foot {{ margin-top:14px; font-size:12px; color:#8e918f; line-height:1.45; }}
</style>
</head>
<body>
  <form class="card" method="POST" action="/oauth/authorize" autocomplete="off">
    <h1>输入机房 6 位配对码</h1>
    <p>查看机房电脑黑色窗口显示的 6 位一次性数字，授权当前 AI 临时接管（下课关窗即刻销毁）。</p>
    {err_block}
    <div class="meta">请求客户端：{html.escape(client_id[:36])}</div>
    <input type="hidden" name="client_id" value="{html.escape(client_id)}">
    <input type="hidden" name="redirect_uri" value="{html.escape(redirect_uri)}">
    <input type="hidden" name="state" value="{html.escape(state)}">
    <input type="hidden" name="code_challenge" value="{html.escape(code_challenge)}">
    <input type="hidden" name="code_challenge_method" value="{html.escape(code_challenge_method)}">
    <input type="hidden" name="scope" value="{html.escape(scope)}">
    <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">
    <input type="text" name="pin" inputmode="numeric" pattern="[0-9\\-]*" maxlength="7" placeholder="000-000" autofocus required>
    <button type="submit">验证并接入机房电脑</button>
    <div class="foot">安全护栏：连续输错 5 次自动作废并刷新机房配对码；机房零账号残留。</div>
  </form>
</body>
</html>"""


class PortalRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hub: HubState = None  # injected on server class

    def log_message(self, fmt, *args):
        # Quiet clean logging without leaking tokens in URLs
        raw_path = (self.path or "").split("?")[0]
        if raw_path.startswith("/files/"):
            raw_path = "/files/<redacted>"
        elif raw_path.startswith("/mcp/"):
            raw_path = "/mcp/<redacted>"
        if raw_path == "/limb/poll":
            return
        print(f"[{self.log_date_time_string()}] {self.command} {raw_path}", flush=True)

    def _client_ip(self) -> str:
        cf_ip = (self.headers.get("CF-Connecting-IP") or "").strip()
        if cf_ip:
            return cf_ip
        xff = (self.headers.get("X-Forwarded-For") or "").strip()
        if xff:
            return xff.split(",")[-1].strip()
        return self.client_address[0] if self.client_address else "unknown"

    def _is_direct_loopback(self) -> bool:
        peer = (self.client_address[0] if self.client_address else "").strip().lower()
        if peer not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            return False
        for proxy_hdr in ("X-Forwarded-For", "X-Forwarded-Host", "CF-Connecting-IP", "X-Real-IP", "Forwarded"):
            if (self.headers.get(proxy_hdr) or "").strip():
                return False
        return True

    def _set_cors(self, path: str):
        if path == "/mcp" or path.startswith("/mcp/") or path.startswith("/.well-known/") or path.startswith("/files/"):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS, HEAD")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type, Authorization, Mcp-Session-Id, mcp-protocol-version, X-Target-Path-B64",
            )
            self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id")
            self.send_header("Access-Control-Max-Age", "86400")

    def parse_request(self):
        self._body_drained = False
        return super().parse_request()

    def _req_base(self) -> str:
        return self.hub.base_url_for(
            self.headers.get("Host", ""),
            self.headers.get("X-Forwarded-Host", ""),
            self.headers.get("X-Forwarded-Proto", ""),
        )

    def _send_bytes(
        self,
        status: int,
        content_type: str,
        body: bytes,
        extra_headers: dict = None,
        path_for_cors: str = "",
        close_conn: bool = False,
    ):
        # CRITICAL HTTP/1.1 Keep-Alive guard: always drain small unread request bodies (e.g. `{}` from
        # `.NET HttpWebRequest` / Cloudflare Tunnel pooling) before replying, otherwise leftover bytes
        # corrupt the next request line on the reused socket (`{}POST /limb/reply` -> 501)!
        if not getattr(self, "_body_drained", False):
            try:
                clen = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                clen = -1
            if 0 < clen <= 65536:
                try:
                    self.rfile.read(clen)
                    self._body_drained = True
                except Exception:
                    self.close_connection = True
            elif clen > 65536 or clen < 0 or self.headers.get("Transfer-Encoding"):
                self.close_connection = True

        if close_conn:
            self.close_connection = True

        try:
            self.send_response(status)
            if path_for_cors:
                self._set_cors(path_for_cors)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if getattr(self, "close_connection", False):
                self.send_header("Connection", "close")
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            pass

    def _send_json(
        self,
        status: int,
        obj: dict,
        extra_headers: dict = None,
        path_for_cors: str = "",
        close_conn: bool = False,
    ):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send_bytes(
            status,
            "application/json; charset=utf-8",
            raw,
            extra_headers=extra_headers,
            path_for_cors=path_for_cors,
            close_conn=close_conn,
        )

    def _read_body(self, max_bytes: int = MAX_JSON_BYTES) -> bytes:
        if getattr(self, "_body_drained", False):
            return b""
        clen = int(self.headers.get("Content-Length") or "0")
        if clen < 0 or clen > max_bytes:
            self.close_connection = True
            raise ValueError(f"Payload exceeds limit ({max_bytes} bytes)")
        self._body_drained = True
        return self.rfile.read(clen) if clen > 0 else b""

    def _extract_bearer(self, path: str, qs: dict) -> str:
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        if path.startswith("/mcp/"):
            return urllib.parse.unquote(path[5:].strip("/"))
        return (qs.get("token") or [""])[0].strip()

    def do_OPTIONS(self):
        parsed = urllib.parse.urlparse(self.path)
        self._send_bytes(HTTPStatus.NO_CONTENT, "text/plain", b"", path_for_cors=parsed.path)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        base = self._req_base()

        # 1. Health & Bootstrap Scripts (`/go` for Windows PowerShell, `/go.py` for POSIX)
        # Send `Connection: close` + immediate flush so tunnels like Pinggy / localhost.run never stall
        if path == "/health":
            with self.hub.lock:
                self.hub.gc_locked()
                active_rooms = len(self.hub.rooms)
            return self._send_json(200, {"ok": True, "service": SERVER_INFO["name"], "activeRooms": active_rooms}, close_conn=True)

        if path == "/go":
            ps_tpl = LIMB_PS1_PATH.read_text("ascii")
            # Inject current Hub base URL as default while keeping 100% pure ASCII + CRLF!
            ps_code = ps_tpl.replace("__DEFAULT_HUB_URL__", base).replace("\r\n", "\n").replace("\n", "\r\n")
            return self._send_bytes(200, "text/plain; charset=us-ascii", ps_code.encode("ascii"), close_conn=True)

        if path == "/go.py":
            py_tpl = LIMB_PY_PATH.read_text("utf-8")
            py_code = py_tpl.replace("__DEFAULT_HUB_URL__", base)
            return self._send_bytes(200, "text/x-python; charset=utf-8", py_code.encode("utf-8"), close_conn=True)

        # 2. OAuth 2.0 Discovery Metadata (`easy-mcp` Web AI Connector compatibility)
        if path == "/.well-known/oauth-protected-resource" or path.startswith("/.well-known/oauth-protected-resource/"):
            return self._send_json(
                200,
                {
                    "resource": f"{base}/mcp",
                    "authorization_servers": [base],
                    "bearer_methods_supported": ["header"],
                    "scopes_supported": ["mcp"],
                },
                path_for_cors=path,
            )

        if path in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
            return self._send_json(
                200,
                {
                    "issuer": base,
                    "authorization_endpoint": f"{base}/oauth/authorize",
                    "token_endpoint": f"{base}/oauth/token",
                    "registration_endpoint": f"{base}/oauth/register",
                    "response_types_supported": ["code"],
                    "grant_types_supported": ["authorization_code", "refresh_token"],
                    "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
                    "code_challenge_methods_supported": ["S256", "plain"],
                    "scopes_supported": ["mcp"],
                },
                path_for_cors=path,
            )

        # 3. OAuth 2.0 6-Digit Pairing Code Gate (`GET /oauth/authorize`)
        if path == "/oauth/authorize":
            client_id = (qs.get("client_id") or [""])[0].strip()
            redirect_uri = (qs.get("redirect_uri") or [""])[0].strip()
            resp_type = (qs.get("response_type") or ["code"])[0].strip()
            state = (qs.get("state") or [""])[0]
            challenge = (qs.get("code_challenge") or [""])[0].strip()
            method = (qs.get("code_challenge_method") or ["S256"])[0].strip()
            scope = (qs.get("scope") or ["mcp"])[0].strip()

            with self.hub.lock:
                client = self.hub.oauth_clients.get(client_id)
            if not client:
                return self._send_json(400, {"error": "invalid_client", "error_description": "Unknown client_id; register via /oauth/register first."})
            if resp_type != "code":
                return self._send_json(400, {"error": "unsupported_response_type"})
            uri_ok = (redirect_uri in client["redirect_uris"]) if client["redirect_uris"] else _valid_redirect_uri(redirect_uri)
            if not uri_ok:
                return self._send_json(400, {"error": "invalid_redirect_uri"})
            if client["auth_method"] == "none" and not challenge:
                return self._send_json(400, {"error": "invalid_request", "error_description": "PKCE code_challenge is required for public clients."})

            csrf = self.hub.make_csrf(f"{client_id}:{redirect_uri}:{challenge}")
            page = _render_authorize_html(client_id, redirect_uri, state, challenge, method, scope, csrf)
            return self._send_bytes(200, "text/html; charset=utf-8", page.encode("utf-8"))

        # 4. File Transfer HTTP API (`GET /files/<token>/<path>`)
        if path.startswith("/files/"):
            return self._handle_file_http("GET", path, qs)

        if path == "/mcp" or path.startswith("/mcp/"):
            return self._send_json(
                405,
                {"error": "Stateless Streamable HTTP MCP: send JSON-RPC via POST."},
                extra_headers={"Allow": "POST, OPTIONS"},
                path_for_cors=path,
            )

        return self._send_json(404, {"error": "Not found"})

    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/files/"):
            return self._handle_file_http("PUT", parsed.path, urllib.parse.parse_qs(parsed.query))
        return self._send_json(404, {"error": "Not found"})

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/files/"):
            return self._handle_file_http("DELETE", parsed.path, urllib.parse.parse_qs(parsed.query))
        if parsed.path == "/mcp" or parsed.path.startswith("/mcp/"):
            return self._send_bytes(204, "text/plain", b"", path_for_cors=parsed.path)
        return self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        base = self._req_base()

        # --- Southbound Classroom Limb Endpoints ---
        if path == "/limb/register":
            try:
                data = json.loads(self._read_body(65536).decode("utf-8") or "{}")
            except Exception as e:
                return self._send_json(400, {"ok": False, "error": str(e)})
            try:
                reg = self.hub.register_limb(
                    data.get("hostname", "Classroom-PC"),
                    data.get("os_info", "Windows"),
                    data.get("default_cwd", "D:\\"),
                )
            except OverflowError as e:
                return self._send_json(429, {"ok": False, "error": str(e)})
            reg["ok"] = True
            reg["mcp_endpoint"] = f"{base}/mcp"
            return self._send_json(200, reg)

        if path == "/limb/poll":
            self._read_body(4096)
            secret = (self.headers.get("X-Limb-Secret") or "").strip()
            res = self.hub.limb_poll(secret)
            code = 200 if res.get("ok") else 404
            return self._send_json(code, res)

        if path == "/limb/reply":
            secret = (self.headers.get("X-Limb-Secret") or "").strip()
            try:
                data = json.loads(self._read_body(MAX_FILE_BYTES + 1048576).decode("utf-8"))
            except Exception as e:
                return self._send_json(400, {"ok": False, "error": str(e)})
            ok = self.hub.limb_reply(secret, str(data.get("task_id") or ""), data.get("result") or {})
            return self._send_json(200 if ok else 404, {"ok": ok})

        if path == "/limb/disconnect":
            self._read_body(4096)
            secret = (self.headers.get("X-Limb-Secret") or "").strip()
            ok = self.hub.limb_disconnect(secret)
            return self._send_json(200, {"ok": ok})

        # --- CLI / Mobile Direct 6-Digit PIN Pairing (`POST /pair`) ---
        if path == "/pair":
            if not self.hub.allow_remote_pair and not self._is_direct_loopback():
                self._read_body(16384)
                return self._send_json(
                    403,
                    {
                        "ok": False,
                        "error": "POST /pair is restricted to local loopback (127.0.0.1); remote clients must use OAuth 2.0 /oauth/authorize.",
                    },
                )
            try:
                data = json.loads(self._read_body(16384).decode("utf-8") or "{}")
            except Exception:
                return self._send_json(400, {"ok": False, "error": "Invalid JSON"})
            ok, room_id, err_msg = self.hub.verify_and_consume_pin(self._client_ip(), str(data.get("pin") or ""))
            if not ok:
                return self._send_json(403, {"ok": False, "error": err_msg})
            toks = self.hub.mint_tokens_for_room(room_id, "cli_direct")
            at = toks["access_token"]
            return self._send_json(
                200,
                {
                    "ok": True,
                    "mcp_url": f"{base}/mcp",
                    "one_paste_mcp_url": f"{base}/mcp/{at}",
                    "bearer_token": at,
                    "files_base_url": f"{base}/files/{at}",
                    "expires_in": toks["expires_in"],
                },
            )

        # --- OAuth 2.0 Dynamic Client Registration (`POST /oauth/register`) ---
        if path == "/oauth/register":
            try:
                meta = json.loads(self._read_body(65536).decode("utf-8") or "{}")
            except Exception:
                return self._send_json(400, {"error": "invalid_client_metadata"}, path_for_cors=path)
            raw_uris = meta.get("redirect_uris") if isinstance(meta.get("redirect_uris"), list) else []
            valid_uris = [u for u in raw_uris if isinstance(u, str) and _valid_redirect_uri(u)]
            if raw_uris and not valid_uris:
                return self._send_json(400, {"error": "invalid_redirect_uri"}, path_for_cors=path)
            req_method = str(meta.get("token_endpoint_auth_method") or "")
            auth_method = req_method if req_method in ("none", "client_secret_post", "client_secret_basic") else (
                "none" if not req_method else "client_secret_post"
            )
            cid = "mcp_" + secrets.token_hex(12)
            csecret = "" if auth_method == "none" else secrets.token_hex(24)
            with self.hub.lock:
                if len(self.hub.oauth_clients) > 500:
                    oldest = sorted(self.hub.oauth_clients.items(), key=lambda kv: kv[1]["created_at"])[:100]
                    for k, _ in oldest:
                        self.hub.oauth_clients.pop(k, None)
                self.hub.oauth_clients[cid] = {
                    "secret_hash": _sha256_hex(csecret) if csecret else "",
                    "redirect_uris": valid_uris,
                    "auth_method": auth_method,
                    "created_at": time.time(),
                }
            out = {
                "client_id": cid,
                "client_id_issued_at": int(time.time()),
                "client_secret_expires_at": 0,
                "redirect_uris": valid_uris,
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": auth_method,
                "scope": "mcp",
            }
            if csecret:
                out["client_secret"] = csecret
            return self._send_json(201, out, path_for_cors=path)

        # --- OAuth 2.0 6-Digit PIN Verification (`POST /oauth/authorize`) ---
        if path == "/oauth/authorize":
            raw_form = self._read_body(32768).decode("utf-8", errors="replace")
            form = {k: v[0] for k, v in urllib.parse.parse_qs(raw_form, keep_blank_values=True).items()}
            client_id = form.get("client_id", "").strip()
            redirect_uri = form.get("redirect_uri", "").strip()
            state = form.get("state", "")
            challenge = form.get("code_challenge", "").strip()
            method = form.get("code_challenge_method", "S256").strip()
            scope = form.get("scope", "mcp").strip()
            csrf_tok = form.get("csrf_token", "").strip()
            pin_input = form.get("pin", "").strip()

            with self.hub.lock:
                client = self.hub.oauth_clients.get(client_id)
            if not client:
                return self._send_json(400, {"error": "invalid_client"})
            uri_ok = (redirect_uri in client["redirect_uris"]) if client["redirect_uris"] else _valid_redirect_uri(redirect_uri)
            if not uri_ok:
                return self._send_json(400, {"error": "invalid_redirect_uri"})
            if not self.hub.verify_csrf(csrf_tok, f"{client_id}:{redirect_uri}:{challenge}"):
                new_csrf = self.hub.make_csrf(f"{client_id}:{redirect_uri}:{challenge}")
                page = _render_authorize_html(client_id, redirect_uri, state, challenge, method, scope, new_csrf, "页面表单已过期，请重新输入配对码。")
                return self._send_bytes(400, "text/html; charset=utf-8", page.encode("utf-8"))

            ok, room_id, err_msg = self.hub.verify_and_consume_pin(self._client_ip(), pin_input)
            if not ok:
                new_csrf = self.hub.make_csrf(f"{client_id}:{redirect_uri}:{challenge}")
                page = _render_authorize_html(client_id, redirect_uri, state, challenge, method, scope, new_csrf, err_msg)
                return self._send_bytes(403, "text/html; charset=utf-8", page.encode("utf-8"))

            code = "ac_" + secrets.token_hex(24)
            with self.hub.lock:
                self.hub.oauth_codes[code] = {
                    "room_id": room_id,
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "challenge": challenge,
                    "method": method or "S256",
                    "exp": time.time() + 180.0,
                }
            sep = "&" if "?" in redirect_uri else "?"
            params = [("code", code), ("iss", base)]
            if state:
                params.append(("state", state))
            loc = redirect_uri + sep + urllib.parse.urlencode(params)
            return self._send_bytes(302, "text/plain", b"Authorized", extra_headers={"Location": loc})

        # --- OAuth 2.0 Token Exchange (`POST /oauth/token`) ---
        if path == "/oauth/token":
            raw_body = self._read_body(65536).decode("utf-8", errors="replace")
            ctype = (self.headers.get("Content-Type") or "").lower()
            if "application/json" in ctype:
                try:
                    form = {k: str(v) for k, v in json.loads(raw_body or "{}").items()}
                except Exception:
                    form = {}
            else:
                form = {k: v[0] for k, v in urllib.parse.parse_qs(raw_body, keep_blank_values=True).items()}

            client_id = form.get("client_id", "").strip()
            secret = form.get("client_secret", "").strip()
            auth_hdr = self.headers.get("Authorization") or ""
            if auth_hdr.startswith("Basic "):
                try:
                    dec = base64.b64decode(auth_hdr[6:]).decode("utf-8")
                    bid, bsec = dec.split(":", 1)
                    client_id = client_id or bid
                    secret = secret or bsec
                except Exception:
                    pass

            grant_type = form.get("grant_type", "authorization_code").strip()
            if grant_type == "refresh_token":
                rt = form.get("refresh_token", "").strip()
                with self.hub.lock:
                    self.hub.gc_locked()
                    rec = self.hub.tokens.get(_sha256_hex(rt))
                    if not rec or rec["type"] != "refresh" or rec["room_id"] not in self.hub.rooms:
                        return self._send_json(401, {"error": "invalid_grant", "error_description": "Expired or disconnected classroom room."})
                    client = self.hub.oauth_clients.get(rec["client_id"])
                    if client and client["auth_method"] != "none" and not _safe_eq(_sha256_hex(secret), client["secret_hash"]):
                        return self._send_json(401, {"error": "invalid_client"})
                    return self._send_json(200, self.hub.mint_tokens_for_room(rec["room_id"], rec["client_id"], keep_refresh=rt))

            code = form.get("code", "").strip()
            with self.hub.lock:
                self.hub.gc_locked()
                crec = self.hub.oauth_codes.pop(code, None)
                if not crec or crec["exp"] < time.time() or crec["room_id"] not in self.hub.rooms:
                    return self._send_json(401, {"error": "invalid_grant", "error_description": "Invalid or expired code."})
                if client_id and crec["client_id"] != client_id:
                    return self._send_json(401, {"error": "invalid_grant", "error_description": "client_id mismatch."})
                r_uri = form.get("redirect_uri", "").strip()
                if r_uri and crec["redirect_uri"] and r_uri != crec["redirect_uri"]:
                    return self._send_json(401, {"error": "invalid_grant", "error_description": "redirect_uri mismatch."})

                client = self.hub.oauth_clients.get(crec["client_id"])
                if client and client["auth_method"] != "none":
                    if not _safe_eq(_sha256_hex(secret), client["secret_hash"]):
                        return self._send_json(401, {"error": "invalid_client"})

                if crec["challenge"]:
                    verifier = form.get("code_verifier", "").strip()
                    if not verifier:
                        return self._send_json(401, {"error": "invalid_grant", "error_description": "Missing PKCE code_verifier."})
                    if crec["method"] == "plain":
                        calc = verifier
                    else:
                        calc = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
                    if not _safe_eq(calc, crec["challenge"]):
                        return self._send_json(401, {"error": "invalid_grant", "error_description": "PKCE verification failed."})

                return self._send_json(200, self.hub.mint_tokens_for_room(crec["room_id"], crec["client_id"]))

        # --- Streamable HTTP MCP Endpoint (`POST /mcp` or `POST /mcp/<token>`) ---
        if path == "/mcp" or path.startswith("/mcp/"):
            tok = self._extract_bearer(path, qs)
            room, valid_tok = self.hub.authenticate_mcp_token(tok)
            if not room:
                www = f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'
                return self._send_json(
                    401,
                    {"error": "unauthorized", "message": "Pair with classroom 6-digit PIN via OAuth or POST /pair."},
                    extra_headers={"WWW-Authenticate": www},
                    path_for_cors=path,
                )
            return self._handle_mcp_post(room, valid_tok, base, path)

        return self._send_json(404, {"error": "Not found"})

    def _handle_mcp_post(self, room: dict, raw_tok: str, base: str, path: str):
        try:
            body = json.loads(self._read_body(MAX_JSON_BYTES).decode("utf-8") or "{}")
        except Exception as e:
            return self._send_json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(e)}}, path_for_cors=path)

        rid = body.get("id")
        method = str(body.get("method") or "")
        params = body.get("params") if isinstance(body.get("params"), dict) else {}

        if method.startswith("notifications/"):
            return self._send_bytes(202, "text/plain", b"", path_for_cors=path)

        if method == "initialize":
            cproto = str(params.get("protocolVersion") or "")
            proto = cproto if cproto in SUPPORTED_MCP_PROTOCOLS else SUPPORTED_MCP_PROTOCOLS[0]
            sid = "ms_" + secrets.token_hex(8)
            return self._send_json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "protocolVersion": proto,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": SERVER_INFO,
                        "instructions": (
                            f"{DEFAULT_INSTRUCTIONS}\n"
                            f"Paired Host: {room['hostname']} ({room['os_info']}), Default CWD: {room['default_cwd']}"
                        ),
                    },
                },
                extra_headers={"Mcp-Session-Id": sid},
                path_for_cors=path,
            )

        if method == "ping":
            return self._send_json(200, {"jsonrpc": "2.0", "id": rid, "result": {}}, path_for_cors=path)

        if method == "tools/list":
            return self._send_json(200, {"jsonrpc": "2.0", "id": rid, "result": {"tools": PORTAL_TOOLS}}, path_for_cors=path)

        if method == "tools/call":
            tname = str(params.get("name") or "")
            targs = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}

            if tname == "file_transfer_info":
                files_base = f"{base}/files/{raw_tok}"
                info = {
                    "ok": True,
                    "paired_host": room["hostname"],
                    "default_cwd": room["default_cwd"],
                    "max_bytes": MAX_FILE_BYTES,
                    "files_base_url": files_base,
                    "usage": {
                        "download": f'curl -fsSL "{files_base}/?path=D:/report.docx" -o report.docx',
                        "upload_zero_bom": f'curl -fsSL -X PUT "{files_base}/?path=D:/done.docx" --data-binary @done.docx',
                        "delete": f'curl -fsSL -X DELETE "{files_base}/?path=D:/temp.tmp"',
                    },
                }
                return self._send_json(
                    200,
                    {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": json.dumps(info, ensure_ascii=False, indent=2)}], "isError": False}},
                    path_for_cors=path,
                )

            if tname not in ("run_command", "start_command", "read_command", "stop_command"):
                return self._send_json(
                    200,
                    {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Unknown tool: {tname}"}},
                    path_for_cors=path,
                )

            timeout_s = float(min(600, max(5, int(targs.get("timeout_sec") or 120)))) + 15.0 if tname == "run_command" else 30.0
            try:
                limb_res = self.hub.dispatch_to_limb(room["room_id"], tname, targs, timeout_sec=timeout_s)
                is_err = not bool(limb_res.get("ok", True))
                return self._send_json(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {
                            "content": [{"type": "text", "text": json.dumps(limb_res, ensure_ascii=False, indent=2)}],
                            "isError": is_err,
                        },
                    },
                    path_for_cors=path,
                )
            except Exception as e:
                return self._send_json(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {
                            "content": [{"type": "text", "text": json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)}],
                            "isError": True,
                        },
                    },
                    path_for_cors=path,
                )

        return self._send_json(
            200,
            {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method not found: {method}"}},
            path_for_cors=path,
        )

    def _handle_file_http(self, method: str, path: str, qs: dict):
        # URL shape: `/files/<token>` with `?path=D:/foo.docx` OR `/files/<token>/relpath`
        rest = path[len("/files/"):]
        parts = rest.split("/", 1)
        tok = urllib.parse.unquote(parts[0])
        subpath = urllib.parse.unquote(parts[1]) if len(parts) > 1 else ""
        target_path = (qs.get("path") or [""])[0].strip() or subpath
        b64_hdr = (self.headers.get("X-Target-Path-B64") or "").strip()
        if b64_hdr:
            try:
                target_path = base64.b64decode(b64_hdr).decode("utf-8")
            except Exception:
                pass

        room, _ = self.hub.authenticate_mcp_token(tok)
        if not room:
            return self._send_json(401, {"ok": False, "error": "Unauthorized file token"}, path_for_cors=path)
        if not target_path:
            return self._send_json(400, {"ok": False, "error": "Missing ?path= or /files/<token>/<path>"}, path_for_cors=path)

        try:
            if method == "GET":
                res = self.hub.dispatch_to_limb(room["room_id"], "file_read", {"path": target_path}, timeout_sec=60.0)
                if not res.get("ok"):
                    return self._send_json(404, res, path_for_cors=path)
                raw_bytes = base64.b64decode(res.get("data_b64") or "")
                fname = os.path.basename(target_path.replace("\\", "/")) or "download.bin"
                ascii_fname = "".join(
                    ch if (32 <= ord(ch) < 127 and ch not in ('"', "\\", ";")) else "_"
                    for ch in fname
                ).strip("_") or "download.bin"
                if ascii_fname == fname:
                    cd_val = f'attachment; filename="{ascii_fname}"'
                else:
                    utf8_fname = urllib.parse.quote(fname, safe="")
                    cd_val = f"attachment; filename=\"{ascii_fname}\"; filename*=UTF-8''{utf8_fname}"
                return self._send_bytes(
                    200,
                    "application/octet-stream",
                    raw_bytes,
                    extra_headers={
                        "Content-Disposition": cd_val,
                        "X-SHA256": hashlib.sha256(raw_bytes).hexdigest(),
                    },
                    path_for_cors=path,
                )

            if method == "PUT":
                raw_bytes = self._read_body(MAX_FILE_BYTES)
                b64_data = base64.b64encode(raw_bytes).decode("ascii")
                res = self.hub.dispatch_to_limb(
                    room["room_id"],
                    "file_write",
                    {"path": target_path, "data_b64": b64_data},
                    timeout_sec=60.0,
                )
                return self._send_json(200 if res.get("ok") else 400, res, path_for_cors=path)

            if method == "DELETE":
                res = self.hub.dispatch_to_limb(room["room_id"], "file_delete", {"path": target_path}, timeout_sec=30.0)
                return self._send_json(200 if res.get("ok") else 400, res, path_for_cors=path)
        except Exception as e:
            return self._send_json(500, {"ok": False, "error": str(e)}, path_for_cors=path)


def create_server(
    host: str = "127.0.0.1",
    port: int = 8787,
    public_url: str = "",
    allow_remote_pair: bool = False,
) -> ThreadingHTTPServer:
    hub = HubState(public_base_url=public_url, allow_remote_pair=allow_remote_pair)

    class BoundHandler(PortalRequestHandler):
        pass

    BoundHandler.hub = hub
    httpd = ThreadingHTTPServer((host, port), BoundHandler)
    httpd.daemon_threads = True
    return httpd


def _stop_by_pidfiles(pidfiles: list[str]) -> int:
    my_pid = os.getpid()
    stopped = []
    for pf in pidfiles:
        if not pf or not os.path.isfile(pf):
            continue
        try:
            pid = int(Path(pf).read_text("utf-8").strip())
        except Exception:
            try:
                os.remove(pf)
            except OSError:
                pass
            continue
        if pid > 1 and pid != my_pid:
            try:
                os.kill(pid, signal.SIGTERM)
                stopped.append(f"{pid} ({pf})")
            except ProcessLookupError:
                pass
            except Exception as e:
                print(f"[WARN] Could not signal PID {pid} from {pf}: {e}", flush=True)
        try:
            os.remove(pf)
        except OSError:
            pass
    if stopped:
        print(f"[STOPPED] Cleanly terminated: {', '.join(stopped)}", flush=True)
    else:
        print("[STOPPED] No active pidfile processes found.", flush=True)
    return 0


def main():
    ap = argparse.ArgumentParser(description="Classroom Portal MCP Hub (5-tool MCP + OAuth 2.0 + 6-digit PIN gate)")
    ap.add_argument("--host", default=os.environ.get("PORTAL_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORTAL_PORT", "8787")))
    ap.add_argument(
        "--public-url",
        default=os.environ.get("PORTAL_PUBLIC_URL", "auto"),
        help="Public HTTPS base URL, or 'auto' (default) to follow incoming Host / X-Forwarded-Host dynamically",
    )
    ap.add_argument(
        "--pidfile",
        default=os.environ.get("PORTAL_PIDFILE", "/tmp/classroom_portal_hub.pid"),
        help="Path to write Hub PID for safe teardown without pkill self-kill (default: /tmp/classroom_portal_hub.pid)",
    )
    ap.add_argument(
        "--tunnel-pidfile",
        default=os.environ.get("PORTAL_TUNNEL_PIDFILE", "/tmp/classroom_portal_tunnel.pid"),
        help="Optional tunnel PID file also cleaned up by --stop (default: /tmp/classroom_portal_tunnel.pid)",
    )
    ap.add_argument("--stop", action="store_true", help="Safely stop running Hub & tunnel via pidfiles (avoids pkill -f self-kill)")
    ap.add_argument(
        "--allow-remote-pair",
        action="store_true",
        help="Allow POST /pair through reverse proxies (disabled by default; remote Web clients use OAuth 2.0 /oauth/authorize)",
    )
    args = ap.parse_args()

    if args.stop:
        raise SystemExit(_stop_by_pidfiles([args.pidfile, args.tunnel_pidfile]))

    try:
        srv = create_server(args.host, args.port, args.public_url, allow_remote_pair=args.allow_remote_pair)
    except OSError as e:
        if e.errno in (errno.EADDRINUSE, 98, 48, 10048):
            print(
                f"[ERROR] Port {args.port} on {args.host} is already in use.\n"
                f"  - To stop an existing Hub: python3 {Path(__file__).name} --stop\n"
                f"  - Or pick another port   : python3 {Path(__file__).name} --port {args.port + 1}",
                flush=True,
            )
            raise SystemExit(2)
        raise

    pid_written = False
    if args.pidfile:
        try:
            Path(args.pidfile).parent.mkdir(parents=True, exist_ok=True)
            Path(args.pidfile).write_text(f"{os.getpid()}\n", encoding="utf-8")
            pid_written = True
        except OSError:
            pass

    pub_clean = (args.public_url or "").strip().rstrip("/")
    base_disp = pub_clean if (pub_clean and pub_clean.lower() != "auto") else "<https://your-tunnel-url>"
    print(f"[READY] Classroom Portal MCP Hub listening on http://{args.host}:{args.port} (pid={os.getpid()}, public_url={args.public_url or 'auto'})", flush=True)
    print(f"  - Windows Classroom Win+R : powershell -NoP -Ep Bypass -c \"irm {base_disp}/go | iex\"", flush=True)
    print(f"  - POSIX Classroom One-Line: curl -fsSL {base_disp}/go.py | python3 -", flush=True)
    print(f"  - Web AI MCP Connector URL: {base_disp}/mcp (OAuth 2.0 gated by 6-digit PIN)", flush=True)
    print(f"  - Safe Teardown           : python3 {Path(__file__).name} --stop", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        if pid_written and args.pidfile and os.path.isfile(args.pidfile):
            try:
                if Path(args.pidfile).read_text("utf-8").strip() == str(os.getpid()):
                    os.remove(args.pidfile)
            except OSError:
                pass


if __name__ == "__main__":
    main()
