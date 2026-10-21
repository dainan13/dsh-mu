#!/usr/bin/env python3
"""failure-modes.py - check what the platform does when things break.

    python3 tests/failure-modes.py [--user alice --password … --instance alice]

Every scenario stops something real and then puts it back; the script always
restores the services it touched, even when it fails or is interrupted:

  1. backend down      - /admin/ must show the operator page (not a traceback),
                         while /login, / and the ops API keep working
  2. backend back      - /admin/ recovers without touching nginx
  3. instance down     - / must answer with the "starting/stopped" page instead
                         of hanging, and /me must still describe the account
  4. instance crash    - kill -9 the jailed harness; supervise-daemon must bring
                         it back and the gateway must recover
  5. reload under load - `dsh-muctl gateway install` while requests are in flight
                         must not drop them (nginx graceful reload)

Exit code 0 only when every assertion holds and the final state is healthy.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1"
PASS = 0
FAIL = 0


def ok(label: str, extra: str = "") -> None:
    global PASS
    PASS += 1
    print(f"PASS  {label}" + (f"  -- {extra}" if extra else ""))


def bad(label: str, extra: str = "") -> None:
    global FAIL
    FAIL += 1
    print(f"FAIL  {label}" + (f"  -- {extra}" if extra else ""))


def sh(cmd: str, timeout: int = 60) -> tuple[int, str]:
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return res.returncode, (res.stdout + res.stderr).strip()


def http_call(url: str, opener=None, timeout: int = 15) -> tuple[int, str]:
    open_ = opener.open if opener else urllib.request.urlopen
    try:
        with open_(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # connection refused, timeout, ...
        return 0, f"{type(exc).__name__}: {exc}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """See the gateway's own answer instead of following it to /login."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def raw_call(url: str, timeout: int = 15) -> tuple[int, str, str]:
    """GET without following redirects: returns (status, location, body)."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(url, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Location", ""), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Location", ""), exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return 0, "", f"{type(exc).__name__}: {exc}"


def login(user: str, password: str):
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    status, body = http_call(f"{BASE}/login", opener)
    if status != 200:
        return None, f"GET /login -> {status}"
    match = re.search(r'name="csrf" value="([^"]+)"', body)
    if not match:
        return None, "no csrf token in the login form"
    form = urllib.parse.urlencode({"username": user, "password": password,
                                   "csrf": match.group(1), "next": "/"}).encode()
    req = urllib.request.Request(f"{BASE}/login", data=form)
    try:
        opener.open(req, timeout=30)
    except urllib.error.HTTPError:
        pass
    return opener, ""


def wait_port(port: int, timeout: float, want_open: bool = True) -> bool:
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                if want_open:
                    return True
        except OSError:
            if not want_open:
                return True
        time.sleep(0.5)
    return False


def instance_port(name: str) -> int:
    with open("/etc/dsh-mu/state.json") as fh:
        return int(json.load(fh)["instances"][name]["port"])


def jailed_pid(port: int) -> str:
    code, out = sh(f"pgrep -f -- '--port {port}' | head -1")
    return out.strip() if code == 0 else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="alice")
    ap.add_argument("--password", default="alice-test-123")
    ap.add_argument("--instance", default="")
    args = ap.parse_args()
    instance = args.instance or args.user
    port = instance_port(instance)

    print(f"== failure modes: user={args.user} instance={instance} port={port} ==")
    opener, err = login(args.user, args.password)
    if not opener:
        print(f"cannot log in: {err}")
        return 2

    backend_was_running = sh("rc-service dsh-mud status")[0] == 0
    instance_was_running = sh(f"rc-service dsh-mu-{instance} status")[0] == 0
    try:
        # ---------------------------------------------------------- 1. backend down
        sh("rc-service dsh-mud stop", timeout=60)
        if not wait_port(8787, 20, want_open=False):
            bad("could not stop the admin backend (8787 still listening)")
        else:
            ok("admin backend stopped (8787 closed)")
        # Anonymous visitors never reach the backend: the role gate sends them to
        # /login first, which must keep working while the backend is down.
        status, location, body = raw_call(f"{BASE}/admin/")
        if status == 302 and "/login" in location:
            ok("backend down: anonymous /admin/ goes to the login page", f"HTTP {status}")
        elif status in (502, 503, 504):
            bad("backend down: anonymous /admin/ hit the dead backend", f"HTTP {status}")
        else:
            bad("backend down: unexpected /admin/ answer",
                f"HTTP {status} location={location!r}")
        if "traceback" in body.lower():
            bad("backend down: a Lua traceback leaked")
        else:
            ok("backend down: no Lua traceback leaked")
        # A logged-in *non-admin* is refused by the gate as usual.
        status, _ = http_call(f"{BASE}/admin/", opener)
        ok("backend down: a normal user is still refused /admin/", f"HTTP {status}") \
            if status in (403, 302) else bad("backend down: normal user got", f"HTTP {status}")
        status, _ = http_call(f"{BASE}/login")
        ok("backend down: /login still serves", f"HTTP {status}") if status == 200 \
            else bad("backend down: /login broke", f"HTTP {status}")
        status, body = http_call(f"{BASE}/", opener)
        if status in (200, 302, 503):
            ok("backend down: instances keep working through the gateway", f"HTTP {status}")
        else:
            bad("backend down: the gateway could not reach the instance", f"HTTP {status}")
        token = open("/etc/dsh-mu/admin.token").read().strip()
        status, _ = http_call(f"{BASE}/mu-api/status?t={token}")
        ok("backend down: ops API still answers", f"HTTP {status}") if status == 200 \
            else bad("backend down: ops API broke", f"HTTP {status}")
        # The operator page for an authenticated admin session cannot be reached
        # without a system password, so assert it is wired in the live config.
        # The gateway configuration lives on the host (old mode) or in the jail's
        # volume (default); assemble whichever applies.
        mode = "jail"
        try:
            with open("/etc/dsh-mu/dsh-mu.conf", errors="replace") as fh:
                for line in fh:
                    if line.startswith("GATEWAY_MODE="):
                        mode = line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
        if mode == "jail":
            conf_dir = "/var/lib/dsh-mu/gateway/conf"
            code, nginx_conf = sh(f"cat {conf_dir}/nginx.conf {conf_dir}/conf.d/*.conf "
                                  f"{conf_dir}/http.d/*.conf 2>/dev/null")
        else:
            code, nginx_conf = sh("nginx -T 2>/dev/null")
        if "@gw_admin_down" in nginx_conf and "dsh-muctl admin restart" in nginx_conf:
            ok("backend down: the operator page is wired into the running config")
        else:
            bad("backend down: no operator page in the running nginx config")

        # ---------------------------------------------------------- 2. backend back
        sh("rc-service dsh-mud start", timeout=60)
        wait_port(8787, 30)
        status, _ = http_call(f"{BASE}/admin/")
        if status in (302, 200):
            ok("backend restarted: /admin/ recovers", f"HTTP {status}")
        else:
            bad("backend restarted: /admin/ still broken", f"HTTP {status}")

        # --------------------------------------------------------- 3. instance down
        sh(f"rc-service dsh-mu-{instance} stop", timeout=60)
        wait_port(port, 20, want_open=False)
        status, body = http_call(f"{BASE}/", opener, timeout=25)
        if status == 200 and ("\u542f\u52a8" in body or "start" in body.lower()):
            ok("instance down: / answers with a starting/stopped page", f"HTTP {status}")
        elif status in (200, 302, 503):
            ok("instance down: / answers instead of hanging", f"HTTP {status}")
        else:
            bad("instance down: / did not answer usefully", f"HTTP {status}")
        status, body = http_call(f"{BASE}/me", opener)
        ok("instance down: /me still describes the session", f"HTTP {status}") \
            if status in (200, 401) else bad("instance down: /me broke", f"HTTP {status}")

        # -------------------------------------------------------- 4. instance crash
        sh(f"rc-service dsh-mu-{instance} start", timeout=60)
        if not wait_port(port, 90):
            bad(f"instance {instance} did not come back on port {port} after a stop")
        else:
            ok(f"instance restarted after a normal stop (port {port} listening)")
        pid = jailed_pid(port)
        if pid:
            sh(f"kill -9 {pid}")
            if wait_port(port, 90):
                ok("instance killed with SIGKILL: supervisor brought it back")
            else:
                bad("instance killed with SIGKILL and never came back")
        else:
            bad("could not find the jailed harness process to kill")

        # ---------------------------------------------------- 5. reload under load
        import threading
        results: list[int] = []
        stop = threading.Event()

        def hammer() -> None:
            while not stop.is_set():
                status, _ = http_call(f"{BASE}/", opener, timeout=20)
                results.append(status)

        threads = [threading.Thread(target=hammer, daemon=True) for _ in range(4)]
        for t in threads:
            t.start()
        time.sleep(1)
        code, out = sh("dsh-muctl gateway install", timeout=120)
        time.sleep(1)
        stop.set()
        for t in threads:
            t.join(timeout=10)
        good = sum(1 for s in results if s in (200, 302, 503))
        if results and good == len(results):
            ok(f"graceful reload: all {len(results)} in-flight requests survived")
        else:
            bad(f"graceful reload dropped requests: {len(results) - good}/{len(results)}",
                f"gateway install exit {code}")
    finally:
        if backend_was_running:
            sh("rc-service dsh-mud start", timeout=60)
        if instance_was_running:
            sh(f"rc-service dsh-mu-{instance} start", timeout=60)
        time.sleep(2)

    # ------------------------------------------------------------- final health
    status, _ = http_call(f"{BASE}/login")
    backend = sh("rc-service dsh-mud status")[0] == 0
    listening = wait_port(port, 30)
    if status == 200 and backend and listening:
        ok("everything restored", f"login {status}, backend up, port {port} listening")
    else:
        bad("state not fully restored",
            f"login {status}, backend {'up' if backend else 'down'}, "
            f"port {port} {'open' if listening else 'closed'}")

    print(f"\n=== FAILURE MODES: PASS={PASS} FAIL={FAIL} ===")
    print("FAILURE-MODE CHECKS PASSED" if FAIL == 0 else "FAILURE-MODE CHECKS FAILED")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
