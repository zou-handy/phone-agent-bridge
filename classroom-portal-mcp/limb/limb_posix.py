#!/usr/bin/env python3
"""
Cross-platform POSIX Limb (`limb_posix.py`) for Classroom Portal MCP.
Used on Linux/macOS lab machines (`curl -fsSL https://<host>/go.py | python3 -`)
and for real end-to-end automated integration verification.
"""
import argparse
import base64
import json
import os
import platform
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

BLOCKED_PREFIXES = ("/etc/shadow", "/etc/gshadow", "/proc/kcore")


def http_json(hub_url: str, path: str, body: dict = None, headers: dict = None, timeout: float = 35.0) -> dict:
    data = json.dumps(body or {}).encode("utf-8")
    hdrs = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(hub_url.rstrip("/") + path, data=data, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", errors="replace")
    return json.loads(raw) if raw else {}


class PosixLimb:
    def __init__(self, hub_url: str, default_cwd: str):
        self.hub_url = hub_url.rstrip("/")
        self.default_cwd = str(Path(default_cwd).resolve())
        self.bg_jobs: dict[str, dict] = {}
        self.limb_secret = ""
        self.pin = ""
        self.running = True

    def _resolve_path(self, raw_path: str) -> Path:
        p = Path(raw_path)
        if not p.is_absolute():
            p = Path(self.default_cwd) / p
        resolved = p.resolve()
        for bp in BLOCKED_PREFIXES:
            if str(resolved).startswith(bp):
                raise PermissionError(f"Blocked sensitive path: {resolved}")
        return resolved

    def run_foreground(self, args: dict) -> dict:
        cmd = str(args.get("command") or "")
        cwd = str(args.get("cwd") or self.default_cwd)
        if not os.path.isdir(cwd):
            cwd = self.default_cwd
        timeout_sec = max(1, min(600, int(args.get("timeout_sec") or 120)))
        shell_bin = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
        p = subprocess.Popen(
            [shell_bin, "-c", cmd],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
        try:
            out, err = p.communicate(timeout=timeout_sec)
            return {"ok": p.returncode == 0, "exit_code": p.returncode, "cwd": cwd, "stdout": out, "stderr": err}
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.communicate()
            return {"ok": False, "timed_out": True, "exit_code": -1, "cwd": cwd, "stdout": "", "stderr": f"Timed out after {timeout_sec}s"}

    def start_bg(self, args: dict) -> dict:
        active = sum(1 for j in self.bg_jobs.values() if j["proc"].poll() is None)
        if active >= 4:
            return {"ok": False, "error": "Max 4 concurrent background commands reached."}
        cmd = str(args.get("command") or "")
        cwd = str(args.get("cwd") or self.default_cwd)
        if not os.path.isdir(cwd):
            cwd = self.default_cwd
        cid = "cmd_" + secrets.token_hex(4)
        log_fd, log_path = tempfile.mkstemp(prefix=f"portal_{cid}_", suffix=".log")
        log_file = os.fdopen(log_fd, "wb")
        shell_bin = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
        proc = subprocess.Popen(
            [shell_bin, "-c", cmd],
            cwd=cwd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log_file.close()
        self.bg_jobs[cid] = {"id": cid, "pid": proc.pid, "command": cmd, "cwd": cwd, "proc": proc, "log_path": log_path}
        return {"ok": True, "command_id": cid, "pid": proc.pid, "cwd": cwd}

    def read_bg(self, args: dict) -> dict:
        cid = str(args.get("command_id") or "").strip()
        if not cid:
            jobs = []
            for j in self.bg_jobs.values():
                rc = j["proc"].poll()
                jobs.append({"command_id": j["id"], "pid": j["pid"], "running": rc is None, "exit_code": rc, "command": j["command"]})
            return {"ok": True, "jobs": jobs}
        j = self.bg_jobs.get(cid)
        if not j:
            return {"ok": False, "error": f"Unknown command_id: {cid}"}
        offset = max(0, int(args.get("offset") or 0))
        txt = Path(j["log_path"]).read_text("utf-8", errors="replace") if os.path.exists(j["log_path"]) else ""
        rc = j["proc"].poll()
        return {
            "ok": True,
            "command_id": cid,
            "pid": j["pid"],
            "running": rc is None,
            "exit_code": rc,
            "output": txt[offset:],
            "next_offset": len(txt),
        }

    def stop_bg(self, args: dict) -> dict:
        cid = str(args.get("command_id") or "").strip()
        j = self.bg_jobs.pop(cid, None)
        if not j:
            return {"ok": False, "error": f"Unknown command_id: {cid}"}
        if j["proc"].poll() is None:
            try:
                os.killpg(j["proc"].pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            j["proc"].wait(timeout=3)
        if os.path.exists(j["log_path"]):
            os.remove(j["log_path"])
        return {"ok": True, "stopped": cid}

    def handle_task(self, op: str, pl: dict) -> dict:
        if op == "run_command":
            return self.run_foreground(pl)
        if op == "start_command":
            return self.start_bg(pl)
        if op == "read_command":
            return self.read_bg(pl)
        if op == "stop_command":
            return self.stop_bg(pl)
        if op == "file_read":
            fp = self._resolve_path(str(pl.get("path") or ""))
            if not fp.is_file():
                return {"ok": False, "error": f"File not found: {fp}"}
            raw = fp.read_bytes()
            return {"ok": True, "bytes": len(raw), "data_b64": base64.b64encode(raw).decode("ascii")}
        if op == "file_write":
            fp = self._resolve_path(str(pl.get("path") or ""))
            fp.parent.mkdir(parents=True, exist_ok=True)
            raw = base64.b64decode(str(pl.get("data_b64") or ""))
            fp.write_bytes(raw)
            return {"ok": True, "path": str(fp), "bytes": len(raw)}
        if op == "file_delete":
            fp = self._resolve_path(str(pl.get("path") or ""))
            if fp.is_file():
                fp.unlink()
            return {"ok": True, "deleted": str(fp)}
        return {"ok": False, "error": f"Unknown op: {op}"}

    def cleanup(self):
        for cid in list(self.bg_jobs.keys()):
            try:
                self.stop_bg({"command_id": cid})
            except Exception:
                pass
        if self.limb_secret:
            try:
                http_json(self.hub_url, "/limb/disconnect", {}, {"X-Limb-Secret": self.limb_secret}, timeout=5.0)
            except Exception:
                pass

    def serve(self, ready_file: str = ""):
        reg = http_json(
            self.hub_url,
            "/limb/register",
            {
                "hostname": socket.gethostname(),
                "os_info": f"{platform.system()} {platform.release()}",
                "default_cwd": self.default_cwd,
            },
            timeout=10.0,
        )
        self.limb_secret = reg["limb_secret"]
        self.pin = reg["pin"]
        print(f"[6-DIGIT PAIRING PIN] >>> {reg['pin_formatted']} <<< (MCP: {reg['mcp_endpoint']})", flush=True)
        if ready_file:
            Path(ready_file).write_text(json.dumps(reg), encoding="utf-8")

        try:
            while self.running:
                try:
                    poll = http_json(self.hub_url, "/limb/poll", {}, {"X-Limb-Secret": self.limb_secret}, timeout=32.0)
                except Exception:
                    time.sleep(0.5)
                    continue
                if not poll.get("ok"):
                    break
                self.pin = poll.get("pin", self.pin)
                for task in poll.get("tasks") or []:
                    tid = task["task_id"]
                    try:
                        res = self.handle_task(task["op"], task.get("payload") or {})
                    except Exception as e:
                        res = {"ok": False, "error": str(e)}
                    try:
                        http_json(self.hub_url, "/limb/reply", {"task_id": tid, "result": res}, {"X-Limb-Secret": self.limb_secret}, timeout=20.0)
                    except Exception:
                        pass
        finally:
            self.cleanup()


def main():
    default_url = os.environ.get("PORTAL_HUB_URL", "__DEFAULT_HUB_URL__")
    if default_url == "__" + "DEFAULT_HUB_URL" + "__":
        default_url = "http://127.0.0.1:8787"
    ap = argparse.ArgumentParser()
    ap.add_argument("--hub", default=default_url)
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--ready-file", default="")
    args = ap.parse_args()

    limb = PosixLimb(args.hub, args.cwd)

    def _on_term(*_):
        limb.running = False
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _on_term)
    try:
        limb.serve(ready_file=args.ready_file)
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
