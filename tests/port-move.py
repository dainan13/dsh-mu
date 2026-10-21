#!/usr/bin/env python3
"""port-move.py - moving an instance to another loopback port, end to end.

    python3 tests/port-move.py

`PATCH /api/instances/<name> {port: N}` is the page's port editor, and it ties
together three claims that had never been exercised together:

  * the port is **validated**: outside PORT_RANGE, already assigned to another
    instance, or already in use -> a clean 400, nothing changes
  * the move is recorded, re-rendered into the instance config, and takes effect
    on the next start (the harness binds the port at startup)
  * the gateway resolves the upstream **per request** (FR-3.2): after the move
    the session's own `/me` reports the new port as live, with no nginx reload

It creates its own account+instance and removes both again.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1"
BACKEND = "http://127.0.0.1:8787"
CLI = os.environ.get("DSHMU_CLI", "/usr/local/sbin/dsh-muctl")
STATE = os.environ.get("DSHMU_STATE", "/etc/dsh-mu/state.json")
TOKEN_PATH = "/etc/dsh-mu/admin.token"
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


def sh(cmd: str) -> tuple[int, str]:
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=240)
    return res.returncode, (res.stdout + res.stderr).strip()


def read_state() -> dict:
    with open(STATE, encoding="utf-8") as fh:
        return json.load(fh)


def free_port(lo: int, hi: int, taken: set[int]) -> int:
    for candidate in range(lo, hi + 1):
        if candidate in taken:
            continue
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", candidate))
            except OSError:
                continue
        return candidate
    return 0


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api(opener, path: str, method: str = "GET", payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{BACKEND}{path}", data=data, method=method)
    req.add_header("X-DSH-MU-Admin", token())
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with opener.open(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8", "replace") or "{}")
        except json.JSONDecodeError:
            return exc.code, {}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)}


def token() -> str:
    with open(TOKEN_PATH, encoding="utf-8") as fh:
        return fh.read().strip()


def new_jar():
    return urllib.request.build_opener(_NoRedirect,
                                      urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def login(opener, user: str, password: str) -> int:
    try:
        with opener.open(f"{BASE}/login", timeout=20) as resp:
            page = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        page = exc.read().decode("utf-8", "replace")
    match = re.search(r'name="csrf" value="([^"]+)"', page)
    if not match:
        return 0
    body = urllib.parse.urlencode({"username": user, "password": password,
                                   "csrf": match.group(1), "next": "/"}).encode()
    req = urllib.request.Request(f"{BASE}/login", data=body)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with opener.open(req, timeout=20) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def me(opener) -> dict:
    try:
        with opener.open(f"{BASE}/me", timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except Exception:  # noqa: BLE001
        return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("must run as root", file=sys.stderr)
        return 2

    user = f"portcheck{os.getpid() % 100000}"
    password = "port-check-password-1"
    print(f"== port move: account={user} ==")

    code, out = sh(f"{CLI} user add {user} --password {password}")
    if code != 0:
        bad("could not create the throwaway account", out[-100:])
        return 1
    ok("created the account with its own instance")

    backend = new_jar()
    try:
        state = read_state()
        inst = state["instances"][user]
        original = int(inst["port"])
        # The platform configuration lives in /etc/dsh-mu/dsh-mu.conf (state.json
        # only holds users and instances), so ask the CLI for the port range.
        rc, out = sh(f"{CLI} config port_range")
        try:
            lo, hi = (int(x) for x in json.loads(out))
        except (ValueError, json.JSONDecodeError):
            bad("could not read the configured port range", out[:80])
            return 1
        taken = {int(i.get("port", 0)) for n, i in state["instances"].items() if n != user}
        target = free_port(lo, hi, taken | {original})
        if not target:
            bad("found no free port in the configured range", f"{lo}-{hi}")
            return 1
        ok("picked a free port inside the range", f"{original} -> {target}")

        status, body = api(backend, f"/api/instances/{user}", method="PATCH", payload={"port": target})
        change = (body or {}).get("port_change") or {}
        if status == 200 and change.get("changed") and int(change.get("from", 0)) == original \
                and int(change.get("to", 0)) == target:
            ok("the port move is accepted and reported", f"{change.get('from')} -> {change.get('to')}")
        else:
            bad("the port move was not reported correctly", f"{status} {str(body)[:120]}")

        state = read_state()
        if int(state["instances"][user]["port"]) == target:
            ok("the record carries the new port")
        else:
            bad("the record still has the old port", str(state["instances"][user].get("port")))

        conf = f"/etc/dsh-mu/instances/{user}.conf"
        if os.path.exists(conf):
            text = open(conf, encoding="utf-8", errors="replace").read()
            if f"PORT={target}" in text:
                ok("the instance config was re-rendered with the new port")
            else:
                bad("the instance config does not mention the new port", conf)
        else:
            bad("the instance config is missing", conf)

        # limits/port endpoint must validate: assigned elsewhere, outside the range
        if taken:
            other_port = sorted(taken)[0]
            status, body = api(backend, f"/api/instances/{user}", method="PATCH",
                               payload={"port": other_port})
            if status == 400:
                ok("moving onto another instance's port is refused", f"HTTP 400 {str(body.get('detail'))[:60]}")
            else:
                bad("the conflict guard did not fire", f"{status} {str(body)[:100]}")
        status, body = api(backend, f"/api/instances/{user}", method="PATCH", payload={"port": 1})
        detail = str((body or {}).get("detail") or "")
        # The message matters: `die()` used to leave only the exit code ("1") here.
        if status == 400 and "range" in detail.lower():
            ok("a port outside the configured range is refused, with the reason",
               f"HTTP 400 {detail[:60]}")
        else:
            bad("the range guard did not fire (or lost its message)",
                f"{status} detail={detail[:80]!r}")

        # take it live: restart and wait for the new port
        sh(f"{CLI} instance restart {user}")
        moved = False
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if port_open(target) and not port_open(original):
                moved = True
                break
            time.sleep(3)
        if moved:
            ok("after a restart the instance listens on the new port only",
               f"{target} open, {original} closed")
        else:
            bad("the instance did not move", f"{target} open={port_open(target)} "
                                             f"{original} closed={not port_open(original)}")

        # the gateway resolves the upstream per request, so the session sees it
        jar = new_jar()
        status = login(jar, user, password)
        payload = me(jar)
        inst_view = (payload or {}).get("instance") or {}
        if status == 302 and int(inst_view.get("port", 0)) == target and inst_view.get("live") is True:
            ok("the gateway resolves the moved port without a reload",
               f"/me reports port={inst_view.get('port')} live={inst_view.get('live')}")
        else:
            bad("the gateway did not pick up the new port", f"{status} {json.dumps(inst_view)[:120]}")
    finally:
        if not args.keep:
            sh(f"{CLI} instance stop {user}")
            sh(f"{CLI} user del {user} --with-instance")

    code, out = sh(f"{CLI} user list")
    if user not in out:
        ok("the throwaway account and instance are gone")
    else:
        bad("the throwaway account is still listed")

    line = ""
    code, out = sh(f"{CLI} doctor")
    for candidate in out.splitlines():
        if candidate.startswith("doctor:"):
            line = candidate
    if "0 FAIL" in line and "0 WARN" in line:
        ok("doctor is clean after the move", line)
    else:
        bad("doctor is not clean after the move", line or "no doctor line")

    print(f"\n=== PORT MOVE: PASS={PASS} FAIL={FAIL} ===")
    print("PORT MOVE PASSED" if FAIL == 0 else "PORT MOVE FAILED")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
