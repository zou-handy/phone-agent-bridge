/**
 * Cloudflare Workers + Durable Objects Edition (`cloudflare/worker.js`)
 * Zero-VPS Serverless Classroom Portal MCP:
 *   - Bind a custom domain in Cloudflare Dashboard (avoids `*.workers.dev` DNS poisoning in Mainland China)
 *   - Single Global Coordinator Durable Object (`PortalHubDO`) holds ephemeral 6-digit PIN rooms,
 *     OAuth 2.0 Dynamic Client Registration + PKCE (`S256`), and dispatches the 5 minimal Portal MCP tools
 *     + `/files/<token>` binary transfer to the classroom PC Limb over HTTPS `:443`.
 */

const PORTAL_TOOLS = [
  {
    name: "run_command",
    description: "Run a foreground shell command on the paired classroom computer (default timeout 120s, max 600s).",
    inputSchema: {
      type: "object",
      properties: {
        command: { type: "string" },
        cwd: { type: "string" },
        shell: { type: "string", enum: ["powershell", "pwsh", "cmd", "bash", "sh"] },
        timeout_sec: { type: "integer", minimum: 1, maximum: 600 }
      },
      required: ["command"],
      additionalProperties: false
    }
  },
  {
    name: "start_command",
    description: "Start a background command (max 4 concurrent jobs). Returns command_id and pid.",
    inputSchema: {
      type: "object",
      properties: { command: { type: "string" }, cwd: { type: "string" }, shell: { type: "string" } },
      required: ["command"],
      additionalProperties: false
    }
  },
  {
    name: "read_command",
    description: "Read incremental output from a background job by offset, or omit command_id to list jobs.",
    inputSchema: {
      type: "object",
      properties: { command_id: { type: "string" }, offset: { type: "integer", minimum: 0 } },
      additionalProperties: false
    }
  },
  {
    name: "stop_command",
    description: "Stop a background job and its process tree.",
    inputSchema: {
      type: "object",
      properties: { command_id: { type: "string" }, force: { type: "boolean" } },
      required: ["command_id"],
      additionalProperties: false
    }
  },
  {
    name: "file_transfer_info",
    description: "Return tokenized HTTP `/files/<token>` upload/download endpoints and curl examples.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false }
  }
];

async function sha256Hex(str) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(str));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function sha256Base64Url(str) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(str));
  let b64 = btoa(String.fromCharCode(...new Uint8Array(buf)));
  return b64.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randHex(bytes = 16) {
  const arr = new Uint8Array(bytes);
  crypto.getRandomValues(arr);
  return [...arr].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function genPin(existing) {
  for (let i = 0; i < 50; i++) {
    const arr = new Uint32Array(1);
    crypto.getRandomValues(arr);
    const pin = String((arr[0] % 900000) + 100000);
    if (!existing.has(pin)) return pin;
  }
  return "654321";
}

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, Mcp-Session-Id, mcp-protocol-version, X-Target-Path-B64",
    "Access-Control-Expose-Headers": "Mcp-Session-Id",
    "Cache-Control": "no-store"
  };
}

function jsonResp(status, obj, extra = {}) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...corsHeaders(), ...extra }
  });
}

export class PortalHubDO {
  constructor(state, env) {
    this.state = state;
    this.env = env;
    this.rooms = new Map();
    this.pinToRoom = new Map();
    this.secretToRoom = new Map();
    this.clients = new Map();
    this.codes = new Map();
    this.tokens = new Map();
  }

  gc() {
    const now = Date.now();
    for (const [rid, r] of this.rooms) {
      if (now - r.lastSeen > 120000) this.destroyRoom(rid);
    }
    for (const [c, v] of this.codes) {
      if (v.exp < now) this.codes.delete(c);
    }
    for (const [th, t] of this.tokens) {
      if ((t.exp && t.exp < now) || !this.rooms.has(t.roomId)) this.tokens.delete(th);
    }
  }

  destroyRoom(rid) {
    const r = this.rooms.get(rid);
    if (!r) return;
    this.rooms.delete(rid);
    this.pinToRoom.delete(r.pin);
    this.secretToRoom.delete(r.secretHash);
    if (r.pollWaiter) r.pollWaiter({ ok: false, error: "room_closed" });
    for (const [, waiter] of r.inflight) waiter.reject(new Error("Classroom disconnected"));
  }

  rotatePin(r, note) {
    this.pinToRoom.delete(r.pin);
    const np = genPin(this.pinToRoom);
    r.pin = np;
    r.strikes = 0;
    r.pinNote = note;
    this.pinToRoom.set(np, r.roomId);
    if (r.pollWaiter) {
      const w = r.pollWaiter;
      r.pollWaiter = null;
      w(this.drainPoll(r));
    }
  }

  drainPoll(r) {
    const note = r.pinNote;
    r.pinNote = "";
    const tasks = r.queue.splice(0, r.queue.length);
    return {
      ok: true,
      pin: r.pin,
      pin_formatted: `${r.pin.slice(0, 3)}-${r.pin.slice(3)}`,
      pin_note: note,
      tasks
    };
  }

  async fetch(request) {
    this.gc();
    const url = new URL(request.url);
    const path = url.pathname;
    const base = `${url.protocol}//${url.host}`;

    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: corsHeaders() });
    if (path === "/health") return jsonResp(200, { ok: true, activeRooms: this.rooms.size });

    if (path === "/.well-known/oauth-protected-resource" || path.startsWith("/.well-known/oauth-protected-resource/")) {
      return jsonResp(200, { resource: `${base}/mcp`, authorization_servers: [base], bearer_methods_supported: ["header"], scopes_supported: ["mcp"] });
    }
    if (path === "/.well-known/oauth-authorization-server" || path === "/.well-known/openid-configuration") {
      return jsonResp(200, {
        issuer: base,
        authorization_endpoint: `${base}/oauth/authorize`,
        token_endpoint: `${base}/oauth/token`,
        registration_endpoint: `${base}/oauth/register`,
        response_types_supported: ["code"],
        grant_types_supported: ["authorization_code", "refresh_token"],
        token_endpoint_auth_methods_supported: ["none", "client_secret_post", "client_secret_basic"],
        code_challenge_methods_supported: ["S256", "plain"]
      });
    }

    if (path === "/limb/register" && request.method === "POST") {
      const body = await request.json().catch(() => ({}));
      const roomId = "rm_" + randHex(8);
      const secret = "ls_" + randHex(24);
      const secretHash = await sha256Hex(secret);
      const pin = genPin(this.pinToRoom);
      const room = {
        roomId,
        pin,
        pinNote: "",
        strikes: 0,
        secretHash,
        hostname: String(body.hostname || "Classroom-PC").slice(0, 64),
        osInfo: String(body.os_info || "Windows").slice(0, 64),
        defaultCwd: String(body.default_cwd || "D:\\").slice(0, 260),
        lastSeen: Date.now(),
        queue: [],
        inflight: new Map(),
        pollWaiter: null
      };
      this.rooms.set(roomId, room);
      this.pinToRoom.set(pin, roomId);
      this.secretToRoom.set(secretHash, roomId);
      return jsonResp(200, { ok: true, room_id: roomId, limb_secret: secret, pin, pin_formatted: `${pin.slice(0, 3)}-${pin.slice(3)}`, mcp_endpoint: `${base}/mcp` });
    }

    if (path === "/limb/poll" && request.method === "POST") {
      const sh = await sha256Hex(request.headers.get("X-Limb-Secret") || "");
      const rid = this.secretToRoom.get(sh);
      const r = rid ? this.rooms.get(rid) : null;
      if (!r) return jsonResp(404, { ok: false, error: "room_not_found" });
      r.lastSeen = Date.now();
      if (r.queue.length > 0 || r.pinNote) return jsonResp(200, this.drainPoll(r));
      const payload = await new Promise((resolve) => {
        r.pollWaiter = resolve;
        setTimeout(() => {
          if (r.pollWaiter === resolve) {
            r.pollWaiter = null;
            resolve(this.drainPoll(r));
          }
        }, 22000);
      });
      return jsonResp(payload.ok ? 200 : 404, payload);
    }

    if (path === "/limb/reply" && request.method === "POST") {
      const sh = await sha256Hex(request.headers.get("X-Limb-Secret") || "");
      const rid = this.secretToRoom.get(sh);
      const r = rid ? this.rooms.get(rid) : null;
      if (!r) return jsonResp(404, { ok: false });
      r.lastSeen = Date.now();
      const body = await request.json();
      const w = r.inflight.get(body.task_id);
      if (w) {
        r.inflight.delete(body.task_id);
        w.resolve(body.result || {});
      }
      return jsonResp(200, { ok: true });
    }

    if (path === "/limb/disconnect" && request.method === "POST") {
      const sh = await sha256Hex(request.headers.get("X-Limb-Secret") || "");
      const rid = this.secretToRoom.get(sh);
      if (rid) this.destroyRoom(rid);
      return jsonResp(200, { ok: Boolean(rid) });
    }

    if (path === "/oauth/register" && request.method === "POST") {
      const meta = await request.json().catch(() => ({}));
      const cid = "mcp_" + randHex(12);
      const authMethod = meta.token_endpoint_auth_method || "none";
      const secret = authMethod === "none" ? "" : randHex(24);
      this.clients.set(cid, { secretHash: secret ? await sha256Hex(secret) : "", redirectUris: meta.redirect_uris || [], authMethod });
      const out = { client_id: cid, redirect_uris: meta.redirect_uris || [], grant_types: ["authorization_code", "refresh_token"], response_types: ["code"], token_endpoint_auth_method: authMethod };
      if (secret) out.client_secret = secret;
      return jsonResp(201, out);
    }

    if (path === "/oauth/authorize" && request.method === "GET") {
      const q = url.searchParams;
      const html = `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>6位配对码授权</title><body style="background:#131314;color:#fff;font-family:system-ui;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0"><form method="POST" action="/oauth/authorize" style="background:#1e1f20;padding:24px;border-radius:16px;max-width:360px;width:90%"><h3>输入机房屏幕 6 位配对码</h3><input type="hidden" name="client_id" value="${q.get("client_id") || ""}"><input type="hidden" name="redirect_uri" value="${q.get("redirect_uri") || ""}"><input type="hidden" name="state" value="${q.get("state") || ""}"><input type="hidden" name="code_challenge" value="${q.get("code_challenge") || ""}"><input type="hidden" name="code_challenge_method" value="${q.get("code_challenge_method") || "S256"}"><input name="pin" maxlength="7" placeholder="000-000" autofocus required style="width:100%;box-sizing:border-box;font-size:24px;text-align:center;letter-spacing:5px;padding:10px;border-radius:10px;background:#131314;color:#fff;border:1px solid #555"><button style="width:100%;margin-top:14px;padding:12px;border-radius:10px;border:0;background:#a8c7fa;color:#062e6f;font-weight:700">验证并连接</button></form></body>`;
      return new Response(html, { status: 200, headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" } });
    }

    if (path === "/oauth/authorize" && request.method === "POST") {
      const fd = await request.formData();
      const pin = String(fd.get("pin") || "").replace(/\D/g, "");
      const rid = this.pinToRoom.get(pin);
      const room = rid ? this.rooms.get(rid) : null;
      if (!room) {
        for (const r of this.rooms.values()) {
          r.strikes += 1;
          if (r.strikes >= 5) this.rotatePin(r, "5 failed attempts — auto-rotated");
        }
        return new Response("Invalid or expired 6-digit PIN.", { status: 403 });
      }
      this.rotatePin(room, "Paired new AI client — rotated PIN");
      const code = "ac_" + randHex(20);
      this.codes.set(code, {
        roomId: rid,
        clientId: String(fd.get("client_id") || ""),
        redirectUri: String(fd.get("redirect_uri") || ""),
        challenge: String(fd.get("code_challenge") || ""),
        method: String(fd.get("code_challenge_method") || "S256"),
        exp: Date.now() + 180000
      });
      const target = new URL(String(fd.get("redirect_uri")));
      target.searchParams.set("code", code);
      if (fd.get("state")) target.searchParams.set("state", String(fd.get("state")));
      target.searchParams.set("iss", base);
      return Response.redirect(target.toString(), 302);
    }

    if (path === "/oauth/token" && request.method === "POST") {
      const txt = await request.text();
      const p = new URLSearchParams(txt);
      const code = p.get("code") || "";
      const crec = this.codes.get(code);
      this.codes.delete(code);
      if (!crec || crec.exp < Date.now() || !this.rooms.has(crec.roomId)) return jsonResp(401, { error: "invalid_grant" });
      if (crec.challenge) {
        const v = p.get("code_verifier") || "";
        const calc = crec.method === "plain" ? v : await sha256Base64Url(v);
        if (calc !== crec.challenge) return jsonResp(401, { error: "invalid_grant" });
      }
      const at = "cp_at_" + randHex(24);
      this.tokens.set(await sha256Hex(at), { roomId: crec.roomId, exp: Date.now() + 43200000, raw: at });
      return jsonResp(200, { access_token: at, token_type: "Bearer", expires_in: 43200, scope: "mcp" });
    }

    if (path === "/mcp" || path.startsWith("/mcp/")) {
      const auth = request.headers.get("Authorization") || "";
      const rawTok = auth.startsWith("Bearer ") ? auth.slice(7).trim() : (path.startsWith("/mcp/") ? path.slice(5) : "");
      const trec = rawTok ? this.tokens.get(await sha256Hex(rawTok)) : null;
      const room = trec ? this.rooms.get(trec.roomId) : null;
      if (!room) {
        return jsonResp(401, { error: "unauthorized" }, { "WWW-Authenticate": `Bearer resource_metadata="${base}/.well-known/oauth-protected-resource"` });
      }
      const rpc = await request.json();
      if (String(rpc.method || "").startsWith("notifications/")) return new Response("", { status: 202, headers: corsHeaders() });
      if (rpc.method === "initialize") {
        return jsonResp(200, { jsonrpc: "2.0", id: rpc.id, result: { protocolVersion: "2025-03-26", capabilities: { tools: { listChanged: false } }, serverInfo: { name: "classroom-portal-mcp", version: "1.0.0" } } });
      }
      if (rpc.method === "tools/list") return jsonResp(200, { jsonrpc: "2.0", id: rpc.id, result: { tools: PORTAL_TOOLS } });
      if (rpc.method === "tools/call") {
        const tname = rpc.params?.name;
        const targs = rpc.params?.arguments || {};
        if (tname === "file_transfer_info") {
          return jsonResp(200, { jsonrpc: "2.0", id: rpc.id, result: { content: [{ type: "text", text: JSON.stringify({ files_base_url: `${base}/files/${rawTok}` }, null, 2) }], isError: false } });
        }
        const taskId = "tk_" + randHex(6);
        const prom = new Promise((resolve, reject) => {
          room.inflight.set(taskId, { resolve, reject });
          setTimeout(() => {
            if (room.inflight.has(taskId)) {
              room.inflight.delete(taskId);
              reject(new Error("Classroom response timeout"));
            }
          }, 125000);
        });
        room.queue.push({ task_id: taskId, op: tname, payload: targs });
        if (room.pollWaiter) {
          const w = room.pollWaiter;
          room.pollWaiter = null;
          w(this.drainPoll(room));
        }
        const res = await prom;
        return jsonResp(200, { jsonrpc: "2.0", id: rpc.id, result: { content: [{ type: "text", text: JSON.stringify(res, null, 2) }], isError: !res.ok } });
      }
    }
    return jsonResp(404, { error: "Not found" });
  }
}

export default {
  async fetch(request, env) {
    const id = env.PORTAL_HUB.idFromName("global-classroom-hub");
    return env.PORTAL_HUB.get(id).fetch(request);
  }
};
