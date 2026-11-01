#!/usr/bin/env python3
"""dsh-mu-broker — the privileged side of cross-end plugins ("service P").

Design notes (see docs/18-跨端插件与资源代理.md):

  * instances hold NO addresses and NO credentials; they only ever learn a
    resource *name* plus a short-lived token;
  * every action is authorised against a **session-scoped mount**, so "only what
    is mounted into the conversation works" is enforced here, not in the plugin;
  * the audit trail is append-only on the host, where an instance cannot touch it.

Authentication is two-step:

  1. bootstrap token, delivered once per instance start through a FIFO
     (`<instance_home>/.dsh-mu/broker.fifo`), single-use, ~5 min TTL;
  2. exchanged at `POST /broker/v1/session` for a session token (12 h TTL) that
     the plugin keeps in memory only.

Nothing in this process executes a shell on behalf of a client: `ssh` is invoked
with an explicit argv, and only against addresses resolved from the catalog.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ETC = "/etc/dsh-mu"
VAR = "/var/lib/dsh-mu"
BROKER_DIR = f"{VAR}/broker"
BROKER_CONF = f"{ETC}/broker.json"
BROKER_STATE = f"{BROKER_DIR}/state.json"
BROKER_AUDIT = "/var/log/dsh-mu/broker-audit.jsonl"
BROKER_KEYS = f"{BROKER_DIR}/keys"

SESSION_TTL = 12 * 3600
BOOTSTRAP_TTL = 300
MAX_MOUNTS = 16
DEFAULT_TIMEOUT = 30

AUDIT_LOCK = threading.Lock()
STATE_LOCK = threading.RLock()


# --------------------------------------------------------------------- helpers
def log(message: str) -> None:
    print(f"[broker] {message}", flush=True)


def load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(path: str, doc) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def audit(event: dict) -> None:
    """Append one line; never let audit failures break a request."""
    event = {"ts": int(time.time()), **event}
    with AUDIT_LOCK:
        try:
            os.makedirs(os.path.dirname(BROKER_AUDIT), exist_ok=True)
            with open(BROKER_AUDIT, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError as exc:  # pragma: no cover
            log(f"audit write failed: {exc}")


def digest(token: str) -> str:
    return hmac.new(b"dsh-mu-broker", token.encode(), "sha256").hexdigest()


def state() -> dict:
    with STATE_LOCK:
        doc = load_json(BROKER_STATE, {})
        doc.setdefault("mounts", {})
        doc.setdefault("sessions", {})
        doc.setdefault("bootstrap", {})
        return doc


def write_state(doc: dict) -> None:
    with STATE_LOCK:
        save_json(BROKER_STATE, doc)


def catalog() -> dict:
    doc = load_json(BROKER_CONF, {})
    return doc.get("resources") or {}


def prune(doc: dict) -> None:
    now = int(time.time())
    for key in [k for k, v in doc["mounts"].items() if int(v.get("expires") or 0) < now]:
        doc["mounts"].pop(key, None)
    for key in [k for k, v in doc["sessions"].items() if int(v.get("expires") or 0) < now]:
        doc["sessions"].pop(key, None)
    for key in [k for k, v in doc["bootstrap"].items() if int(v.get("expires") or 0) < now]:
        doc["bootstrap"].pop(key, None)


# ------------------------------------------------------------------- identity
def authenticate(header: str) -> tuple[dict | None, str]:
    """Return (identity, error). Identity: {user, instance, kind, ttl}."""
    token = ""
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
    if not token:
        return None, "missing bearer token"
    doc = state()
    with STATE_LOCK:
        prune(doc)
        boot = doc["bootstrap"].pop(digest(token), None)
        if boot is not None:
            write_state(doc)
            return ({"user": boot["user"], "instance": boot["instance"],
                     "kind": "bootstrap", "plugin": boot.get("plugin", "")}, "")
        sess = doc["sessions"].get(digest(token))
        if sess is not None:
            return ({"user": sess["user"], "instance": sess["instance"],
                     "kind": "session", "session": sess.get("session", ""),
                     "plugin": sess.get("plugin", "")}, "")
    return None, "unknown or expired token"


def visible_resources(identity: dict) -> dict:
    out = {}
    for name, row in sorted(catalog().items()):
        owner = str(row.get("owner") or "")
        if owner in ("*", identity["user"]) or identity["user"] in (row.get("allow") or []):
            out[name] = {"id": name, "kind": row.get("kind") or "ssh",
                         "caps": row.get("caps") or ["ssh:exec"],
                         "label": row.get("note") or name}
    return out


def grant_resource(identity: dict, name: str) -> dict:
    row = catalog().get(name)
    if not row:
        raise BrokerError(404, "unknown_resource",
                          f"资源 {name} 不存在", "请让管理员在 catalog 里添加该资源")
    owner = str(row.get("owner") or "")
    if not (owner in ("*", identity["user"]) or identity["user"] in (row.get("allow") or [])):
        raise BrokerError(403, "not_your_resource",
                          f"资源 {name} 不属于你", "请让管理员把资源分配给你的账号")
    return row


def mount_for(identity: dict, mount_id: str, cap: str) -> dict:
    doc = state()
    with STATE_LOCK:
        prune(doc)
        entry = doc["mounts"].get(mount_id)
    if not entry:
        raise BrokerError(404, "no_such_mount", "该挂载不存在或已过期",
                          "请在面板重新挂载该资源")
    if entry["instance"] != identity["instance"]:
        raise BrokerError(403, "not_your_mount", "该挂载属于别的实例", "不要共用挂载 id")
    if cap and cap not in (entry.get("caps") or []):
        raise BrokerError(403, "capability_denied", f"该挂载没有 {cap} 权限",
                          "请在面板上勾选对应能力后重新挂载")
    return entry


# ---------------------------------------------------------------------- errors
class BrokerError(Exception):
    def __init__(self, status: int, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.status, self.code, self.message, self.hint = status, code, message, hint


# ----------------------------------------------------------------- ssh runner
SSH_BASE = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR"]


def run_ssh(resource: dict, command: str, timeout: int) -> dict:
    address = str(resource.get("address") or "")
    user = str(resource.get("user") or "root")
    key = str(resource.get("key") or "")
    host, _, port = address.partition(":")
    if not host:
        raise BrokerError(500, "bad_resource", "资源没有配置地址", "请管理员检查 catalog")
    if key and not os.path.exists(key):
        raise BrokerError(500, "missing_key", f"密钥不存在：{key}", "请管理员恢复该密钥文件")
    argv = list(SSH_BASE)
    if port:
        argv += ["-p", port]
    if key:
        argv += ["-i", key]
    argv += [f"{user}@{host}", "--", command]
    started = time.time()
    try:
        res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return {"stdout": res.stdout[-64000:], "stderr": res.stderr[-16000:],
                "exit": res.returncode, "seconds": round(time.time() - started, 3)}
    except subprocess.TimeoutExpired:
        raise BrokerError(504, "timeout", f"命令超过 {timeout}s 未返回",
                          "缩短命令或提高 timeout_ms") from None


# ------------------------------------------------------------------ HTTP layer
class Handler(BaseHTTPRequestHandler):
    server_version = "dsh-mu-broker/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep stderr for real problems
        return

    # -- plumbing ----------------------------------------------------------
    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 1_000_000:
            raise BrokerError(413, "body_too_large", "请求体过大", "只发必要字段")
        raw = self.rfile.read(length)
        try:
            doc = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise BrokerError(400, "bad_json", "请求体不是合法 JSON", "") from None
        if not isinstance(doc, dict):
            raise BrokerError(400, "bad_json", "请求体必须是对象", "")
        return doc

    def _handle(self, method: str) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if path in ("/health", "/admin"):
                self._route(method, {"user": "-", "instance": "-", "kind": "probe"})
                return
            identity, err = authenticate(self.headers.get("Authorization") or "")
            if identity is None:
                raise BrokerError(401, "unauthorized", err, "让插件先做令牌交换")
            self._route(method, identity)
        except BrokerError as exc:
            self._json(exc.status, {"error": {"code": exc.code,
                                              "message": exc.message,
                                              "hint": exc.hint}})
        except BrokenPipeError:
            return
        except Exception as exc:  # noqa: BLE001 - never leak a traceback
            log(f"internal error: {exc}")
            self._json(500, {"error": {"code": "internal", "message": "内部错误", "hint": ""}})

    def do_GET(self):  # noqa: N802
        self._handle("GET")

    def do_POST(self):  # noqa: N802
        self._handle("POST")

    def do_DELETE(self):  # noqa: N802
        self._handle("DELETE")

    # -- routes ------------------------------------------------------------
    def _route(self, method: str, identity: dict) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        plugin = self.headers.get("X-DSHMU-Plugin") or identity.get("plugin") or ""

        if method == "GET" and path == "/health":
            self._json(200, {"ok": True, "service": "dsh-mu-ssh-p",
                             "version": "0.1.0-example"})
            return

        if method == "GET" and path in ("/health", "/admin"):
            body = ADMIN_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if method == "GET" and path == "/broker/v1/whoami":
            self._json(200, {"user": identity["user"], "instance": identity["instance"],
                             "plugin": plugin, "kind": identity["kind"]})
            return

        if method == "GET" and path == "/broker/v1/resources":
            self._json(200, {"resources": list(visible_resources(identity).values())})
            return

        if method == "POST" and path == "/broker/v1/session":
            body = self._body()
            doc = state()
            token = secrets.token_urlsafe(32)
            session = str(body.get("session") or "")
            with STATE_LOCK:
                prune(doc)
                doc["sessions"][digest(token)] = {
                    "user": identity["user"], "instance": identity["instance"],
                    "session": session, "plugin": str(body.get("plugin") or plugin),
                    "expires": int(time.time()) + SESSION_TTL,
                }
                write_state(doc)
            audit({"kind": "session", "instance": identity["instance"],
                   "user": identity["user"], "plugin": plugin, "session": session,
                   "ok": True})
            self._json(200, {"token": token, "expires_in": SESSION_TTL})
            return

        if method == "POST" and path == "/broker/v1/mounts":
            body = self._body()
            resource_name = str(body.get("resource") or "")
            session = str(body.get("session") or "")
            if not session:
                raise BrokerError(400, "missing_session", "缺少 session",
                                  "把当前会话 id 一起发过来")
            row = grant_resource(identity, resource_name)
            allowed_caps = row.get("caps") or ["ssh:exec"]
            wanted = [c for c in (body.get("caps") or allowed_caps) if c in allowed_caps]
            if not wanted:
                raise BrokerError(403, "capability_denied",
                                  "申请的能力不在该资源允许范围内",
                                  "只申请 " + ", ".join(allowed_caps))
            doc = state()
            with STATE_LOCK:
                prune(doc)
                mine = [m for m in doc["mounts"].values()
                        if m["instance"] == identity["instance"]]
                if len(mine) >= MAX_MOUNTS:
                    raise BrokerError(429, "too_many_mounts",
                                      f"这个实例最多同时挂载 {MAX_MOUNTS} 个资源",
                                      "先卸载不用的资源")
            mount_id = "m-" + secrets.token_hex(8)
            ttl = int(body.get("ttl") or SESSION_TTL)
            ttl = max(60, min(ttl, 12 * 3600))
            entry = {"mount": mount_id, "instance": identity["instance"],
                     "user": identity["user"], "session": session,
                     "resource": resource_name, "caps": wanted,
                     "approval": bool(body.get("approval")),
                     "plugin": plugin, "created": int(time.time()),
                     "expires": int(time.time()) + ttl}
            with STATE_LOCK:
                doc["mounts"][mount_id] = entry
                write_state(doc)
            audit({"kind": "mount", **{k: entry[k] for k in
                                       ("instance", "user", "session", "resource",
                                        "caps", "plugin")}, "mount": mount_id,
                   "ok": True})
            self._json(200, {"mount": mount_id, "resource": resource_name,
                             "caps": wanted, "expires_in": ttl,
                             "label": row.get("note") or resource_name})
            return

        m = re.match(r"^/broker/v1/mounts/([\w.-]+)$", path)
        if method == "DELETE" and m:
            mount_id = m.group(1)
            entry = mount_for(identity, mount_id, "")
            doc = state()
            with STATE_LOCK:
                doc["mounts"].pop(mount_id, None)
                write_state(doc)
            audit({"kind": "unmount", "instance": identity["instance"],
                   "user": identity["user"], "session": entry.get("session", ""),
                   "resource": entry.get("resource", ""), "mount": mount_id,
                   "plugin": plugin, "ok": True})
            self._json(200, {"ok": True})
            return

        m = re.match(r"^/broker/v1/mounts/([\w.-]+)/ssh/exec$", path)
        if method == "POST" and m:
            mount_id = m.group(1)
            entry = mount_for(identity, mount_id, "ssh:exec")
            body = self._body()
            command = str(body.get("command") or "")
            if not command.strip():
                raise BrokerError(400, "missing_command", "缺少 command", "传要执行的命令")
            if len(command) > 64000:
                raise BrokerError(413, "command_too_long", "命令过长", "拆成多条")
            timeout = int(body.get("timeout_ms") or DEFAULT_TIMEOUT * 1000) // 1000
            timeout = max(1, min(timeout, 600))
            cwd = str(body.get("cwd") or "")
            if cwd:
                command = f"cd {shell_quote(cwd)} && {command}"
            row = grant_resource(identity, entry["resource"])
            result = run_ssh(row, command, timeout)
            audit({"kind": "ssh_exec", "instance": identity["instance"],
                   "user": identity["user"], "session": entry.get("session", ""),
                   "resource": entry["resource"], "mount": mount_id,
                   "plugin": plugin, "exit": result["exit"],
                   "seconds": result["seconds"], "ok": result["exit"] == 0})
            self._json(200, result)
            return

        raise BrokerError(404, "no_route", f"没有这个接口：{path}",
                          "参见 docs/18-跨端插件与资源代理.md §4.5")


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def listen_port() -> int:
    """Read BROKER_LISTEN from the shell-style /etc/dsh-mu/dsh-mu.conf."""
    try:
        with open(f"{ETC}/dsh-mu.conf", encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r'^\s*BROKER_LISTEN\s*=\s*"?(\d+)"?', line)
                if m:
                    return int(m.group(1))
    except OSError:
        pass
    return 8790


def main() -> int:
    os.makedirs(BROKER_DIR, exist_ok=True)
    os.makedirs(BROKER_KEYS, exist_ok=True)
    try:
        os.chmod(BROKER_KEYS, 0o700)
    except OSError:
        pass
    port = listen_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    log(f"listening on 127.0.0.1:{port} (catalog {BROKER_CONF})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

ADMIN_HTML = """<!doctype html><meta charset="utf-8">
<title>SSH 资源（示例扩展）</title>
<style>body{font:14px/1.6 system-ui;margin:0;padding:16px;background:#0f1218;color:#e6e8eb}
h3{margin:0 0 8px;font-size:14px}code{background:#1b212b;padding:1px 5px;border-radius:4px}
.muted{color:#8b949e;font-size:12px}table{border-collapse:collapse;width:100%;margin-top:8px}
td,th{border-bottom:1px solid #232a35;padding:6px 8px;text-align:left;font-size:13px}
</style>
<h3>SSH 资源（示例扩展的配置页）</h3>
<div class="muted">本页由扩展自己的 jail 提供，平台通过 <code>/ext/ssh/</code> 代理进来。
真实实现在这里做「资源增删改 / 谁可挂载 / 审计查看」，并遵守 docs/18 的面板约定。</div>
<table><thead><tr><th>资源</th><th>能力</th><th>归属</th></tr></thead><tbody id="rows"></tbody></table>
<script>
// Relative URLs only: this page is served behind /ext/<name>/ on the gateway.
fetch('broker/v1/resources')
  .then(r => r.ok ? r.json() : Promise.reject(new Error(r.status)))
  .then(d => {
    document.getElementById('rows').innerHTML = (d.resources || []).length
      ? d.resources.map(x => '<tr><td>' + x.id + '</td><td>' + (x.caps || []).join(', ')
          + '</td><td>' + (x.label || '') + '</td></tr>').join('')
      : '<tr><td colspan="3" class="muted">还没有资源</td></tr>';
  })
  .catch(e => {
    document.getElementById('rows').innerHTML =
      '<tr><td colspan="3" class="muted">读取失败（' + e.message + '）</td></tr>';
  });
</script>
"""
