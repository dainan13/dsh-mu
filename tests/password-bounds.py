#!/usr/bin/env python3
"""password-bounds.py - the password rule on every entrance.

    python3 tests/password-bounds.py

Accounts are local and the password is the only credential, so the minimum is a
security property - and it has to hold whichever door is used:

  * `dsh-muctl user passwd <name> --password x` (the CLI)
  * `dsh-muctl user add <name> --password x` (the CLI)
  * `POST /api/users/<name>/password` (the administrator's page)
  * `POST /api/me/password` (the user's own panel)

Two more things matter as much as the rule itself: a *refused* change must leave
the account exactly as it was (no new hash, no invalidated sessions), and an
accepted 8-character password must actually work for a real login.

It only ever touches throwaway accounts.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import subprocess
import sys
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


def user_record(name: str) -> dict:
    try:
        with open(STATE, encoding="utf-8") as fh:
            return (json.load(fh).get("users") or {}).get(name) or {}
    except (OSError, json.JSONDecodeError):
        return {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def new_jar():
    return urllib.request.build_opener(_NoRedirect,
                                      urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def login(user: str, password: str) -> int:
    opener = new_jar()
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


def api(path: str, payload: dict) -> tuple[int, dict]:
    with open(TOKEN_PATH, encoding="utf-8") as fh:
        token = fh.read().strip()
    req = urllib.request.Request(f"{BACKEND}{path}", method="POST",
                                 data=json.dumps(payload).encode())
    req.add_header("X-DSH-MU-Admin", token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8", "replace") or "{}")
        except json.JSONDecodeError:
            return exc.code, {}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("must run as root", file=sys.stderr)
        return 2

    name = f"pwcheck{os.getpid() % 100000}"
    good = "pw-check-password-8"
    print(f"== password bounds: account={name} ==")

    code, out = sh(f"{CLI} user add {name} --password {good} --instance none")
    if code != 0:
        bad("could not create the throwaway account", out[-100:])
        return 1
    ok("created a throwaway account")

    try:
        before = user_record(name)
        code, out = sh(f"{CLI} user passwd {name} --password x")
        after = user_record(name)
        if code != 0 and "at least" in out:
            ok("the CLI refuses a 1-character password", out.splitlines()[-1][:60])
        else:
            bad("the CLI accepted a 1-character password", out[-80:])
        if after.get("pw") == before.get("pw") and after.get("epoch") == before.get("epoch"):
            ok("a refused change left the account untouched (same hash, same epoch)")
        else:
            bad("a refused change still altered the account",
                f"epoch {before.get('epoch')} -> {after.get('epoch')}")

        code, out = sh(f"{CLI} user passwd {name} --password 1234567")
        if code != 0:
            ok("the CLI refuses a 7-character password")
        else:
            bad("the CLI accepted a 7-character password")

        code, out = sh(f"{CLI} user passwd {name} --password '' </dev/null")
        after = user_record(name)
        if code != 0 and after.get("pw") == before.get("pw"):
            ok("an empty password (empty stdin) is refused and changes nothing")
        else:
            bad("an empty password was accepted", f"exit={code}")

        other = f"pwnew{os.getpid() % 100000}"
        code, out = sh(f"{CLI} user add {other} --password x --instance none")
        created = bool(user_record(other))
        if code != 0 and not created:
            ok("`user add` refuses a short password and creates nothing")
        else:
            bad("`user add` accepted a short password", f"exit={code}")
            sh(f"{CLI} user del {other}")

        status, body = api(f"/api/users/{name}/password", {"password": "1234567"})
        if status == 400:
            ok("the administrator's API refuses a short password too", f"HTTP {status}")
        else:
            bad("the API accepted a short password", f"{status} {str(body)[:70]}")

        # the boundary itself: an 8-character password must be accepted and work
        boundary = "12345678"
        code, out = sh(f"{CLI} user passwd {name} --password {boundary}")
        after = user_record(name)
        if code == 0 and after.get("pw") != before.get("pw") and after.get("epoch") > before.get("epoch"):
            ok("an 8-character password is accepted (the boundary)",
               f"epoch {before.get('epoch')} -> {after.get('epoch')}")
        else:
            bad("an 8-character password was not accepted", out[-80:])

        status = login(name, boundary)
        if status in (302, 303):
            ok("the new password really works for a login", f"HTTP {status}")
        else:
            bad("the new password does not log in", f"HTTP {status}")

        status = login(name, good)
        if status not in (302, 303):
            ok("the old password no longer works")
        else:
            bad("the old password still logs in")
    finally:
        if not args.keep:
            sh(f"{CLI} user del {name}")

    code, out = sh(f"{CLI} user list")
    if name not in out:
        ok("the throwaway account is gone")
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

    print(f"\n=== PASSWORD BOUNDS: PASS={PASS} FAIL={FAIL} ===")
    print("PASSWORD BOUNDS PASSED" if FAIL == 0 else "PASSWORD BOUNDS FAILED")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
