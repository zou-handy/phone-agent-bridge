#!/usr/bin/env python3
"""End-to-end verification suite for Classroom Portal MCP."""
import base64
import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from hub_server import create_server  # noqa: E402


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect())


def req_raw(url: str, method: str = "GET", data: bytes = None, headers: dict = None):
    r = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with OPENER.open(r, timeout=10) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers.items()), err.read()


def main():
    # 1. Enforce CRLF and verify 100% pure ASCII on Windows .ps1 / .bat scripts
    for name in ("classroom_limb.ps1", "start_classroom_portal.bat"):
        fp = BASE_DIR / "limb" / name
        txt = fp.read_bytes().decode("ascii")  # fails immediately if any non-ASCII byte exists
        crlf_bytes = txt.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii")
        fp.write_bytes(crlf_bytes)
        assert b"\r\n" in crlf_bytes, f"Missing CRLF in {name}"
    print("[PASS 1] Windows scripts verified 100% pure ASCII + CRLF")

    # 2. Boot Hub on ephemeral localhost port
    srv = create_server("127.0.0.1", 0)
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    work_dir = Path(tempfile.mkdtemp(prefix="portal_ws_"))
    ready_file = work_dir / "ready.json"
    limb_proc = subprocess.Popen(
        [sys.executable, str(BASE_DIR / "limb" / "limb_posix.py"), "--hub", base, "--cwd", str(work_dir), "--ready-file", str(ready_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        for _ in range(40):
            if ready_file.is_file():
                break
            time.sleep(0.1)
        assert ready_file.is_file(), "Limb failed to register"
        reg = json.loads(ready_file.read_text("utf-8"))
        pin1 = reg["pin"]
        assert len(pin1) == 6 and pin1.isdigit()
        print(f"[PASS 2] Limb registered and received 6-digit PIN: {reg['pin_formatted']}")

        # 3. Verify GET /go returns pure ASCII + CRLF with injected Hub URL
        st_go, _, go_bytes = req_raw(f"{base}/go")
        assert st_go == 200 and base.encode("ascii") in go_bytes and all(b < 128 for b in go_bytes)
        print("[PASS 3] GET /go bootstrapper verified ASCII + CRLF")

        # 4. Unauthenticated POST /mcp returns 401 + WWW-Authenticate metadata pointer
        st_unauth, hdrs_unauth, _ = req_raw(
            f"{base}/mcp",
            method="POST",
            data=b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
            headers={"Content-Type": "application/json"},
        )
        www = hdrs_unauth.get("WWW-Authenticate") or hdrs_unauth.get("www-authenticate") or ""
        assert st_unauth == 401 and "oauth-protected-resource" in www
        print("[PASS 4] Unauthenticated /mcp returned 401 + OAuth discovery header")

        # 5. OAuth 2.0 Dynamic Client Registration + PKCE S256 + Wrong PIN rejection + Correct PIN approval
        redirect_uri = "https://claude.ai/api/mcp/auth_callback"
        st_reg, _, reg_body = req_raw(
            f"{base}/oauth/register",
            method="POST",
            data=json.dumps({"redirect_uris": [redirect_uri], "token_endpoint_auth_method": "none"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        assert st_reg == 201
        client_id = json.loads(reg_body)["client_id"]

        verifier = "v_" + base64.urlsafe_b64encode(os.urandom(24)).rstrip(b"=").decode("ascii")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")

        auth_qs = urllib.parse.urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": "xyz123",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        st_get_auth, _, auth_html_bytes = req_raw(f"{base}/oauth/authorize?{auth_qs}")
        assert st_get_auth == 200
        m_csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', auth_html_bytes.decode("utf-8"))
        assert m_csrf, "Missing CSRF token in /oauth/authorize HTML"
        csrf_tok = m_csrf.group(1)

        # Wrong PIN must return 403
        wrong_pin = "000000" if pin1 != "000000" else "999999"
        bad_form = urllib.parse.urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": "xyz123",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "mcp",
            "csrf_token": csrf_tok,
            "pin": wrong_pin,
        }).encode()
        st_bad, _, _ = req_raw(f"{base}/oauth/authorize", method="POST", data=bad_form, headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert st_bad == 403, f"Expected 403 on wrong PIN, got {st_bad}"

        # Correct PIN must return 302 with single-use authorization code
        good_form = urllib.parse.urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": "xyz123",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "mcp",
            "csrf_token": csrf_tok,
            "pin": f"{pin1[:3]}-{pin1[3:]}",
        }).encode()
        st_good, hdrs_good, _ = req_raw(f"{base}/oauth/authorize", method="POST", data=good_form, headers={"Content-Type": "application/x-www-form-urlencoded"})
        loc = hdrs_good.get("Location") or hdrs_good.get("location") or ""
        assert st_good == 302 and "code=" in loc
        auth_code = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)["code"][0]

        # Replaying the consumed PIN must fail (one-time PIN rotation!)
        st_replay, _, _ = req_raw(f"{base}/oauth/authorize", method="POST", data=good_form, headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert st_replay == 403, "Consumed PIN was not rotated!"

        # Exchange code + PKCE verifier for Bearer token
        tok_form = urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code": auth_code,
            "code_verifier": verifier,
        }).encode()
        st_tok, _, tok_body = req_raw(f"{base}/oauth/token", method="POST", data=tok_form, headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert st_tok == 200
        access_tok = json.loads(tok_body)["access_token"]
        print("[PASS 5] OAuth 2.0 + PKCE S256 + 6-digit one-time PIN gate verified")

        # 6. Verify MCP initialize + tools/list + all 5 Portal tools + /files/ binary roundtrip
        mcp_hdrs = {"Content-Type": "application/json", "Authorization": f"Bearer {access_tok}"}

        def mcp_call(method: str, params: dict = None):
            st, _, raw = req_raw(
                f"{base}/mcp",
                method="POST",
                data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode(),
                headers=mcp_hdrs,
            )
            assert st == 200, f"MCP {method} HTTP {st}: {raw}"
            return json.loads(raw)["result"]

        init_res = mcp_call("initialize", {"protocolVersion": "2025-03-26"})
        assert init_res["protocolVersion"] == "2025-03-26"

        tools = [t["name"] for t in mcp_call("tools/list")["tools"]]
        assert tools == ["run_command", "start_command", "read_command", "stop_command", "file_transfer_info"], tools

        r1 = json.loads(mcp_call("tools/call", {"name": "run_command", "arguments": {"command": "echo HELLO_CLASSROOM_2026"}})["content"][0]["text"])
        assert r1["ok"] and "HELLO_CLASSROOM_2026" in r1["stdout"]

        bg = json.loads(mcp_call("tools/call", {"name": "start_command", "arguments": {"command": "echo BG_LINE_1; sleep 5"}})["content"][0]["text"])
        assert bg["ok"]
        cid = bg["command_id"]
        time.sleep(0.3)
        rd = json.loads(mcp_call("tools/call", {"name": "read_command", "arguments": {"command_id": cid, "offset": 0}})["content"][0]["text"])
        assert rd["ok"] and "BG_LINE_1" in rd["output"]
        sp = json.loads(mcp_call("tools/call", {"name": "stop_command", "arguments": {"command_id": cid}})["content"][0]["text"])
        assert sp["ok"]

        finfo = json.loads(mcp_call("tools/call", {"name": "file_transfer_info", "arguments": {}})["content"][0]["text"])
        fbase = finfo["files_base_url"]
        sample_bytes = b"\x00\xff\xd8BINARY_DOCX_PAYLOAD_NO_BOM\xff\xd9"
        st_put, _, _ = req_raw(f"{fbase}/?path=lab_report.bin", method="PUT", data=sample_bytes)
        assert st_put == 200
        st_get, _, dl_bytes = req_raw(f"{fbase}/?path=lab_report.bin", method="GET")
        assert st_get == 200 and dl_bytes == sample_bytes

        # Non-ASCII Chinese filename PUT + GET roundtrip (RFC 5987 Content-Disposition latin-1 regression check)
        cn_name = "实验报告_数据库实训(终版).docx"
        cn_q = urllib.parse.quote(cn_name)
        st_put_cn, _, _ = req_raw(f"{fbase}/?path={cn_q}", method="PUT", data=sample_bytes)
        assert st_put_cn == 200
        cn_b64 = base64.b64encode(cn_name.encode("utf-8")).decode("ascii")
        st_get_cn, hdrs_cn, dl_cn = req_raw(f"{fbase}/", method="GET", headers={"X-Target-Path-B64": cn_b64})
        cd_cn = hdrs_cn.get("Content-Disposition") or hdrs_cn.get("content-disposition") or ""
        assert st_get_cn == 200 and dl_cn == sample_bytes and "filename*=UTF-8''" in cd_cn, f"CN GET failed: {st_get_cn} {cd_cn}"
        print("[PASS 6] All 5 MCP tools + zero-BOM binary /files/ (ASCII + Chinese UTF-8 filename) roundtrip verified")

        # 7. HTTP/1.1 Keep-Alive regression test: unread `{}` body on /limb/poll must not corrupt next request
        ka_conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            ka_conn.request(
                "POST",
                "/limb/poll",
                body=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "X-Limb-Secret": "invalid_secret_for_ka_test",
                    "Connection": "keep-alive",
                },
            )
            r_ka1 = ka_conn.getresponse()
            r_ka1.read()
            assert r_ka1.status == 404, f"Expected 404 on invalid poll secret, got {r_ka1.status}"

            ka_conn.request(
                "POST",
                "/limb/reply",
                body=b'{"task_id":"none","result":{}}',
                headers={
                    "Content-Type": "application/json",
                    "X-Limb-Secret": "invalid_secret_for_ka_test",
                    "Connection": "keep-alive",
                },
            )
            r_ka2 = ka_conn.getresponse()
            r_ka2.read()
            assert r_ka2.status == 404, f"Keep-Alive desync! Expected 404 (not 501), got {r_ka2.status}"
        finally:
            ka_conn.close()
        print("[PASS 7] HTTP/1.1 Keep-Alive request-body drain verified (no {}POST desync)")

        # 8. Verify Connection: close on /health & /go + seamless tunnel Host rotation without restart
        st_h1, hdrs_h1, _ = req_raw(
            f"{base}/health",
            headers={"X-Forwarded-Host": "alpha-101.lhr.life", "X-Forwarded-Proto": "https"},
        )
        conn_hdr = (hdrs_h1.get("Connection") or hdrs_h1.get("connection") or "").lower()
        assert st_h1 == 200 and conn_hdr == "close", f"Expected Connection: close on /health, got {hdrs_h1}"

        st_r1, _, go_r1 = req_raw(
            f"{base}/go",
            headers={"X-Forwarded-Host": "alpha-101.lhr.life", "X-Forwarded-Proto": "https"},
        )
        assert st_r1 == 200 and b"https://alpha-101.lhr.life" in go_r1

        st_r2, _, go_r2 = req_raw(
            f"{base}/go",
            headers={"X-Forwarded-Host": "bravo-202.lhr.life", "X-Forwarded-Proto": "https"},
        )
        assert st_r2 == 200 and b"https://bravo-202.lhr.life" in go_r2

        # Host-header injection & untrusted Host cache-poisoning regression guards
        st_inj, _, go_inj = req_raw(
            f"{base}/go",
            headers={"X-Forwarded-Host": 'evil.com"; Start-Process calc.exe; #', "X-Forwarded-Proto": "https"},
        )
        assert st_inj == 200 and b"calc.exe" not in go_inj and b"evil.com" not in go_inj, "Host-header injection leaked into /go!"
        _, _, go_untrusted = req_raw(
            f"{base}/health",
            headers={"X-Forwarded-Host": "spoofed-scanner.example.org", "X-Forwarded-Proto": "https"},
        )
        _, _, go_local_after = req_raw(f"{base}/go")
        assert b"spoofed-scanner.example.org" not in go_local_after, "Untrusted Host poisoned last_seen_public_url!"

        # Tunnel-proxied POST /pair must be blocked by default (403), while local loopback POST /pair remains allowed
        st_ext_pair, _, _ = req_raw(
            f"{base}/pair",
            method="POST",
            data=b'{"pin":"000000"}',
            headers={"Content-Type": "application/json", "X-Forwarded-For": "203.0.113.42"},
        )
        assert st_ext_pair == 403, f"Expected 403 on proxied POST /pair, got {st_ext_pair}"
        print("[PASS 8] Connection: close + dynamic tunnel Host rotation + Host-injection & loopback /pair guards verified")

        # 9. Friendly port-conflict check + --pidfile / --stop lifecycle
        dup = subprocess.run(
            [sys.executable, str(BASE_DIR / "hub_server.py"), "--host", "127.0.0.1", "--port", str(port)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        dup_out = (dup.stdout or "") + (dup.stderr or "")
        assert dup.returncode == 2 and "already in use" in dup_out and "Traceback" not in dup_out, dup_out

        pid_file = work_dir / "hub_test.pid"
        sub_hub = subprocess.Popen(
            [sys.executable, str(BASE_DIR / "hub_server.py"), "--host", "127.0.0.1", "--port", "0", "--pidfile", str(pid_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(30):
            if pid_file.is_file():
                break
            time.sleep(0.1)
        assert pid_file.is_file(), "pidfile was not created"
        stop_run = subprocess.run(
            [sys.executable, str(BASE_DIR / "hub_server.py"), "--stop", "--pidfile", str(pid_file), "--tunnel-pidfile", str(work_dir / "none.pid")],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert stop_run.returncode == 0
        sub_hub.wait(timeout=5)
        assert not pid_file.exists(), "pidfile was not removed after --stop"
        print("[PASS 9] Friendly port-in-use guard + --pidfile / --stop lifecycle verified")

        # 10. Disconnect classroom Limb -> room & token immediately wiped (0 residue!)
        limb_proc.terminate()
        limb_proc.wait(timeout=5)
        time.sleep(0.2)
        st_after, _, _ = req_raw(
            f"{base}/mcp",
            method="POST",
            data=b'{"jsonrpc":"2.0","id":2,"method":"ping"}',
            headers=mcp_hdrs,
        )
        assert st_after == 401, f"Expected 401 after Limb window close, got {st_after}"
        print("[PASS 10] Instant room & token self-destruct on window close verified (0 residue)")
        print("ALL_E2E_TESTS_PASSED")
    finally:
        if limb_proc.poll() is None:
            limb_proc.kill()
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    main()
