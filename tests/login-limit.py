#!/usr/bin/env python3
"""login-limit.py - does the failed-login limit actually engage?

    python3 tests/login-limit.py

A single wrong password (selftest check 3) cannot tell a real rate limit apart
from no limit at all, so this drives a throwaway account over the threshold and
checks both the 429 and the lockout semantics.

IMPORTANT: it deliberately burns failures from the *per-client-IP* budget
(LOGIN_LIMIT_IP = 40 per 600s in gateway/lua/login.lua). It therefore runs LAST in
the regression, and skips itself (exit 0 with a clear SKIP) when the budget is
already exhausted - which is what happens when other suites have been failing
logins inside the same window.

Written in Python because a stock Alpine has neither curl nor cookie support in
busybox wget, and the login form is double-submit CSRF protected.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1"
CLI = os.environ.get("DSHMU_CLI", "/usr/local/sbin/dsh-muctl")
PASS = 0
FAIL = 0
SKIP = 0


def ok(label: str, extra: str = "") -> None:
    global PASS
    PASS += 1
    print(f"PASS  {label}" + (f"  -- {extra}" if extra else ""))


def bad(label: str, extra: str = "") -> None:
    global FAIL
    FAIL += 1
    print(f"FAIL  {label}" + (f"  -- {extra}" if extra else ""))


def skip(label: str, extra: str = "") -> None:
    global SKIP
    SKIP += 1
    print(f"SKIP  {label}" + (f"  -- {extra}" if extra else ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def sh(cmd: str) -> int:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).returncode


def new_jar():
    return urllib.request.build_opener(_NoRedirect,
                                      urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def attempt(opener, user: str, password: str) -> int:
    """One login POST (following the CSRF handshake). Returns the status code."""
    try:
        with opener.open(f"{BASE}/login", timeout=15) as resp:
            page = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        page = exc.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return 0
    match = re.search(r'name="csrf" value="([^"]+)"', page)
    if not match:
        return 0
    body = urllib.parse.urlencode({"username": user, "password": password,
                                   "csrf": match.group(1), "next": "/"}).encode()
    req = urllib.request.Request(f"{BASE}/login", data=body)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with opener.open(req, timeout=15) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:  # noqa: BLE001
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--attempts", type=int, default=12)
    args = ap.parse_args()

    user = f"limcheck{os.getpid() % 100000}"
    password = "limit-check-password-1"
    print(f"== login limit: account={user} ==")

    if sh(f"{CLI} user add {user} --password {password} --instance none") != 0:
        bad("could not create the throwaway account")
        print(f"\n=== LOGIN LIMIT: PASS={PASS} FAIL={FAIL} SKIP={SKIP} ===")
        return 1
    ok("created the throwaway account")

    consumed = 1
    try:
        first = attempt(new_jar(), user, "wrong-0")
        if first == 429:
            skip("the per-client-IP budget is already exhausted (429 on the first try)")
            skip("...rerun this layer standalone after the 600s window, or fix the failing layer first")
        else:
            locked_after = 0
            jar = new_jar()
            for i in range(1, args.attempts + 1):
                consumed += 1
                if attempt(jar, user, f"wrong-{i}") == 429:
                    locked_after = i
                    break
            if locked_after:
                ok("failed logins are refused with 429 once the limit is reached",
                   f"after {locked_after} attempts")
            else:
                bad("the login limit never engaged", f"still not refused after {args.attempts} attempts")
            consumed += 1
            right = attempt(new_jar(), user, password)
            if right == 429:
                ok("the lockout holds even for the right password", "HTTP 429 (window is 600s)")
            else:
                bad("the right password got through during the lockout", f"HTTP {right}")
    finally:
        sh(f"{CLI} user del {user}")

    if sh(f"{CLI} user list") == 0:
        listed = subprocess.run(f"{CLI} user list", shell=True, capture_output=True, text=True).stdout
        if user in listed:
            bad("the throwaway account is still listed")
        else:
            ok("the throwaway account is gone")

    print(f"      note: consumed ~{consumed} of the 40 per-client-IP failures (600s window)")
    print(f"\n=== LOGIN LIMIT: PASS={PASS} FAIL={FAIL} SKIP={SKIP} ===")
    print("LOGIN LIMIT PASSED" if FAIL == 0 else "LOGIN LIMIT FAILED")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
