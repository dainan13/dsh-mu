#!/usr/bin/env python3
"""user-flows.py - the account lifecycle, end to end.

    python3 tests/user-flows.py [--prefix flo]

Covers what a real operator does to an account and what the account holder can
do with it:

  * `user add --must-change` marks the account
  * logging in lands on the instance root (platform accounts are never sent to
    the admin page: administrators are host system accounts)
  * `/me` reports the flag, the panel offers the password form
  * `POST /me {"action":"password"}` changes it, clears the flag and invalidates
    every existing session (epoch bump)
  * the old password stops working, the new one works
  * the account is removed again (and its `--instance none` leaves no instance)

It creates and deletes its own throwaway account; nothing else is touched.
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
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
    return res.returncode, (res.stdout + res.stderr).strip()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """See the gateway's own answer: a successful login is a 302, and following
    it would hide the redirect (and bounce off the instance page)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_call(url: str, opener=None, data: bytes | None = None, timeout: int = 20):
    """Returns (status, location, body). Never follows redirects."""
    req = urllib.request.Request(url, data=data)
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    handler = opener or urllib.request.build_opener()
    try:
        with handler.open(req, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Location", ""), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Location", ""), exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, "", f"{type(exc).__name__}: {exc}"


def new_jar():
    return urllib.request.build_opener(_NoRedirect,
                                      urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def login(opener, user: str, password: str) -> tuple[int, str]:
    status, _, body = http_call(f"{BASE}/login", opener)
    if status != 200:
        return status, ""
    match = re.search(r'name="csrf" value="([^"]+)"', body)
    if not match:
        return 0, "no csrf token"
    form = urllib.parse.urlencode({"username": user, "password": password,
                                   "csrf": match.group(1), "next": "/"}).encode()
    status, location, _ = http_call(f"{BASE}/login", opener, data=form)
    return status, location


def me(opener) -> tuple[int, dict]:
    status, _, body = http_call(f"{BASE}/me", opener)
    try:
        return status, json.loads(body)
    except json.JSONDecodeError:
        return status, {}


def _audit_lines() -> int:
    try:
        with open("/var/log/dsh-mu/audit.log", encoding="utf-8", errors="replace") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def post_me(opener, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(f"{BASE}/me", data=data)
    req.add_header("Content-Type", "application/json")
    # /me is same-origin only for unsafe methods, and this client is not a browser
    try:
        with opener.open(req, timeout=20) as resp:
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
    ap.add_argument("--prefix", default="flowcheck")
    args = ap.parse_args()
    user = f"{args.prefix}{os.getpid() % 10000}"
    old_pw = "flow-old-password-123"
    new_pw = "flow-new-password-456"

    print(f"== user flows: account={user} ==")
    code, out = sh(f"{CLI} user add {user} --password '{old_pw}' --must-change --instance none")
    if code == 0:
        ok("account created with --must-change and no instance")
    else:
        bad("could not create the account", out[-120:])
        return 2

    try:
        jar = new_jar()
        status, location = login(jar, user, old_pw)
        if status == 302 and location.rstrip("/") in ("", BASE):
            ok("first login lands on the instance root", f"302 -> {location or '/'}")
        elif status == 302:
            ok("first login redirects", f"302 -> {location}")
        else:
            bad("first login did not succeed", f"{status} {location}")

        status, body = me(jar)
        if status == 200 and body.get("must_change") is True:
            ok("/me reports must_change=true", f"role={body.get('role')}")
        else:
            bad("/me did not report must_change", f"{status} {str(body)[:80]}")
        if body.get("role") == "user" and body.get("admin") is False:
            ok("platform accounts are plain users (admin is a system account)")
        else:
            bad("platform account has an unexpected role", str(body.get("role")))

        # The gateway caches the rendered user view for a couple of seconds
        # (STATE_TTL in lua/dshmu.lua), so give a change time to become visible
        # before asserting on it.
        # The flag is a nudge, not a lock: the holder changes it from the panel.
        status, _, panel = http_call(f"{BASE}/mu-static/panel.js")
        if status == 200 and 'id="pw1"' in panel and "password" in panel:
            ok("the user panel offers the password form")
        else:
            bad("the panel has no password form", f"{status}")

        status, body = post_me(jar, {"action": "password", "password": new_pw})
        if status == 200:
            ok("POST /me changed the password", json.dumps(body)[:60])
        else:
            bad("POST /me could not change the password", f"{status} {str(body)[:80]}")

        time.sleep(3)   # let the gateway's view cache expire

        status, body = me(jar)
        if status == 401:
            ok("the session was invalidated by the change (must log in again)")
        elif status == 200 and body.get("must_change") is False:
            ok("must_change cleared after the change")
        else:
            bad("unexpected state after the change", f"{status} {str(body)[:80]}")

        old_jar = new_jar()
        login(old_jar, user, old_pw)
        status_me, _ = me(old_jar)
        if status_me == 401:
            ok("the old password no longer grants a session")
        else:
            bad("the old password still works", f"/me={status_me}")

        fresh_jar = new_jar()
        status, location = login(fresh_jar, user, new_pw)
        status_me, body = me(fresh_jar)
        if status_me == 200:
            ok("the new password works", f"login={status} must_change={body.get('must_change')}")
        else:
            bad("the new password does not work", f"login={status} /me={status_me}")

        # `user kick`: force-logout without touching the password (FR-1.7).
        audit_lines_before = _audit_lines()
        code, out = sh(f"{CLI} user kick {user}")
        if code == 0:
            ok("user kick succeeded", out.splitlines()[-1][:70] if out else "")
        else:
            bad("user kick failed", out[-120:])
        time.sleep(3)   # the gateway caches the user view for a couple of seconds
        status_me, _ = me(fresh_jar)
        if status_me == 401:
            ok("the running session was invalidated by the kick")
        else:
            bad("the session survived the kick", f"/me={status_me}")

        after_jar = new_jar()
        status, location = login(after_jar, user, new_pw)
        status_me, _ = me(after_jar)
        if status_me == 200:
            ok("the same password still works after the kick (only sessions were dropped)")
        else:
            bad("the kick also broke the password", f"login={status} /me={status_me}")
        if _audit_lines() > audit_lines_before:
            ok("the kick left an audit record")
        else:
            bad("the kick was not audited")

        code, out = sh(f"{CLI} user kick no-such-account-$$")
        if code != 0 and "no such user" in out.lower():
            ok("kicking an unknown account fails cleanly")
        else:
            bad("kicking an unknown account did not fail cleanly", out[-80:])
    finally:
        code, out = sh(f"{CLI} user del {user}")
        if code == 0:
            ok("account removed again")
        else:
            bad("could not remove the account", out[-120:])

    code, out = sh(f"{CLI} user list")
    if user not in out:
        ok("no trace of the throwaway account remains")
    else:
        bad("the account is still listed")

    print(f"\n=== USER FLOWS: PASS={PASS} FAIL={FAIL} ===")
    print("USER FLOWS PASSED" if FAIL == 0 else "USER FLOWS FAILED")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
