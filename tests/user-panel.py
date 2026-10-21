#!/usr/bin/env python3
"""user-panel.py - the page a normal user actually sees.

    python3 tests/user-panel.py

The in-page panel (gateway/static/panel.js) is the only UI a normal account has:
it shows the instance and can start it. Both halves were untested - the rendering
contract (`/me` fields the panel reads) and the start action.

  1. create a throwaway account with its own instance, then stop it
  2. log in through the gateway and check every field the panel renders exists
     and has the type it expects (a missing field silently renders "undefined")
  3. use the panel's start button path (`POST /me {action:"start-instance"}`)
     and wait for the instance to really come up
  4. send an unknown action and require a clean 400
  5. put everything back (instance stopped and deleted, account deleted)

It logs in as a normal user (not an administrator), so it exercises the user-side
of the gateway rather than the ops API.
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
CLI = os.environ.get("DSHMU_CLI", "/usr/local/sbin/dsh-muctl")
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def new_jar():
    return urllib.request.build_opener(_NoRedirect,
                                      urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def call(opener, path: str, method: str = "GET", payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with opener.open(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8", "replace") or "{}")
        except json.JSONDecodeError:
            return exc.code, {}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)}


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


def port_open(port: int) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("must run as root", file=sys.stderr)
        return 2

    user = f"panelcheck{os.getpid() % 100000}"
    password = "panel-check-password-1"
    print(f"== user panel: account={user} ==")

    code, out = sh(f"{CLI} user add {user} --password {password}")
    if code != 0:
        bad("could not create the throwaway account", out[-100:])
        return 1
    ok("created the account with its own instance")

    try:
        # stop it first: the panel's start button only matters when it is down
        sh(f"{CLI} instance stop {user}")
        time.sleep(2)
        # Read the port from the record, the way tests/lib.sh does: the table
        # output of `instance status` is for humans, not for parsing.
        port = 0
        try:
            with open("/etc/dsh-mu/state.json", encoding="utf-8") as fh:
                port = int((json.load(fh).get("instances", {}).get(user, {}) or {}).get("port") or 0)
        except (OSError, ValueError, json.JSONDecodeError):
            port = 0
        if port and not port_open(port):
            ok("the instance is stopped before the test", f"port {port} closed")
        else:
            bad("could not get the instance into a stopped state", f"port={port}")

        jar = new_jar()
        status = login(jar, user, password)
        if status == 302:
            ok("a normal user can log in")
        else:
            bad("login failed", f"HTTP {status}")

        status, me = call(jar, "/me")
        inst = (me or {}).get("instance") if isinstance(me, dict) else None
        if status == 200 and isinstance(inst, dict):
            ok("GET /me returns the instance block the panel renders")
        else:
            bad("GET /me did not return an instance block", f"{status} {str(me)[:100]}")
            inst = inst or {}

        # Arrays must stay JSON arrays. cjson encodes an empty Lua table as `{}`,
        # which made `p.models.filter(...)` throw in the drawer and blanked the panel.
        plans = (me or {}).get("plans")
        providers = (me or {}).get("providers")
        bad_lists = []
        if not isinstance(plans, list):
            bad_lists.append(f"plans is {type(plans).__name__}")
        else:
            for p in plans:
                for field in ("models", "effective_models"):
                    if not isinstance(p.get(field), list):
                        bad_lists.append(f"{p.get('id')}.{field} is {type(p.get(field)).__name__}")
                avail = p.get("available_models")
                if avail is not None and not isinstance(avail, list):
                    bad_lists.append(f"{p.get('id')}.available_models is {type(avail).__name__}")
        if providers is not None and not isinstance(providers, list):
            bad_lists.append(f"providers is {type(providers).__name__}")
        if not bad_lists:
            ok("the drawer's list fields are arrays (empty ones included)",
               f"{len(plans or [])} plan(s)")
        else:
            bad("the drawer's list fields are not arrays", "; ".join(bad_lists[:4]))

        resources = (inst or {}).get("resources")
        if isinstance(resources, dict) and "mem_used" in resources:
            ok("GET /me carries live cgroup resources", f"mem={resources.get('mem_used')}")
        else:
            bad("GET /me has no resources block", str(resources)[:80])

        # Every field panel.js reads, with the type it renders it as.
        checks = [("user", str), ("role", str), ("must_change", bool)]
        missing = [n for n, t in checks if not isinstance((me or {}).get(n), t)]
        if not missing:
            ok("the identity fields the panel reads are present",
               f"user={me.get('user')} role={me.get('role')}")
        else:
            bad("the panel would render these as undefined", ", ".join(missing))

        inst_checks = [("name", str), ("port", int), ("state", str), ("live", bool),
                       ("provider", str), ("model", str)]
        missing = [n for n, t in inst_checks if not isinstance(inst.get(n), t)]
        if not missing:
            ok("the instance fields the panel reads are present and typed right",
               f"{inst.get('name')}:{inst.get('port')} {inst.get('state')} "
               f"provider={inst.get('provider')!r} live={inst.get('live')}")
        else:
            bad("the panel would render these as undefined/wrong", ", ".join(missing))

        # the panel hides the start button when `live` is true, so this must be false
        if inst.get("live") is False:
            ok("the stopped instance reports live=false (so the start button shows)")
        else:
            bad("the stopped instance does not report live=false", str(inst.get("live")))

        # ---------------------------------------------------- the start button
        status, body = call(jar, "/me", method="POST", payload={"action": "start-instance"})
        if status == 202 and isinstance(body, dict) and body.get("accepted"):
            ok("POST /me {action:start-instance} is accepted", f"HTTP {status}")
        else:
            bad("the start action was not accepted", f"{status} {str(body)[:100]}")

        started = False
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if port and port_open(port):
                started = True
                break
            time.sleep(3)
        if started:
            ok("the instance really started after the panel action", f"port {port} open")
        else:
            bad("the instance never came up after the start action", f"port {port}")

        status, me = call(jar, "/me")
        inst = (me or {}).get("instance") or {}
        if inst.get("live") is True:
            ok("/me now reports live=true (the panel would show 运行中)")
        else:
            bad("/me still reports the instance as not live", str(inst.get("live")))

        status, body = call(jar, "/me", method="POST", payload={"action": "definitely-not-an-action"})
        if status == 400 and isinstance(body, dict) and body.get("error") == "unknown action":
            ok("an unknown panel action is refused with a clear 400",
               f"{body.get('error')} / {body.get('action')}")
        else:
            bad("an unknown action was not refused cleanly", f"{status} {str(body)[:100]}")
    finally:
        if not args.keep:
            sh(f"{CLI} instance stop {user}")
            sh(f"{CLI} user del {user} --with-instance")

    code, out = sh(f"{CLI} user list")
    if user not in out:
        ok("the throwaway account and its instance are gone")
    else:
        bad("the throwaway account is still listed")

    line = ""
    code, out = sh(f"{CLI} doctor")
    for candidate in out.splitlines():
        if candidate.startswith("doctor:"):
            line = candidate
    if "0 FAIL" in line and "0 WARN" in line:
        ok("doctor is clean after the drill", line)
    else:
        bad("doctor is not clean after the drill", line or "no doctor line")

    print(f"\n=== USER PANEL: PASS={PASS} FAIL={FAIL} ===")
    print("USER PANEL PASSED" if FAIL == 0 else "USER PANEL FAILED")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
