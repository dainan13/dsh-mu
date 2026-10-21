#!/usr/bin/env python3
"""log-bounds.py - the log endpoint's edges.

    python3 tests/log-bounds.py

The page asks for `?lines=200`, and the tail is clamped to 1..2000 - but the
*parsing* was a bare `int(query[...])`, and the route never checked that the
instance exists. Both are error-quality problems an operator hits by typo:

  * `?lines=abc` must be a clean 400 naming the field, not a 500 "ValueError"
  * an unknown instance must be 404, like every other per-instance route
  * the clamp itself must hold: 0/-5 -> 1 line, a huge number -> at most 2000

It creates a throwaway instance to have a real log path, and leaves everything
as it found it.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

BACKEND = "http://127.0.0.1:8787"
CLI = os.environ.get("DSHMU_CLI", "/usr/local/sbin/dsh-muctl")
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


def call(query: str) -> tuple[int, dict]:
    try:
        with open(TOKEN_PATH, encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError:
        return 0, {}
    req = urllib.request.Request(f"{BACKEND}/api/instances/{INSTANCE}/logs{query}")
    req.add_header("X-DSH-MU-Admin", token)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8", "replace") or "{}")
        except json.JSONDecodeError:
            return exc.code, {}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)}


def call_path(path: str, instance: str) -> tuple[int, dict]:
    try:
        with open(TOKEN_PATH, encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError:
        return 0, {}
    req = urllib.request.Request(f"{BACKEND}/api/instances/{instance}/logs{path}")
    req.add_header("X-DSH-MU-Admin", token)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8", "replace") or "{}")
        except json.JSONDecodeError:
            return exc.code, {}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)}


INSTANCE = ""


def main() -> int:
    global INSTANCE
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("must run as root", file=sys.stderr)
        return 2

    INSTANCE = f"logcheck{os.getpid() % 100000}"
    print(f"== log bounds: instance={INSTANCE} ==")

    code, out = sh(f"{CLI} user add {INSTANCE} --password log-check-password-1")
    if code != 0:
        bad("could not create the throwaway instance", out[-100:])
        return 1
    ok("created a throwaway instance")

    try:
        # give the log some content so the clamp is observable
        log = f"/var/log/dsh-mu/{INSTANCE}.log"
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "w") as fh:
            for i in range(2500):
                fh.write(f"line {i}\n")
        ok("wrote a 2500-line log to tail")

        status, body = call("?lines=200")
        n = len((body or {}).get("lines") or [])
        if status == 200 and 0 < n <= 200:
            ok("?lines=200 returns a bounded tail", f"{n} lines")
        else:
            bad("?lines=200 misbehaved", f"{status} lines={n}")

        status, body = call("?lines=999999")
        n = len((body or {}).get("lines") or [])
        if status == 200 and n <= 2000:
            ok("a huge line count is clamped", f"{n} lines (cap 2000)")
        else:
            bad("the clamp did not hold", f"{status} lines={n}")

        status, body = call("?lines=0")
        n = len((body or {}).get("lines") or [])
        if status == 200 and n == 1:
            ok("?lines=0 is clamped up to 1 line (documented behaviour)", f"{n} line")
        else:
            bad("?lines=0 behaved unexpectedly", f"{status} lines={n}")

        status, body = call("?lines=-5")
        n = len((body or {}).get("lines") or [])
        if status == 200 and n == 1:
            ok("a negative line count is clamped to 1 line", f"{n} line")
        else:
            bad("a negative line count misbehaved", f"{status} lines={n}")

        status, body = call("?lines=abc")
        if status == 400 and isinstance(body, dict) and "lines" in json.dumps(body):
            ok("a non-numeric line count is a clean 400 naming the field",
               f"HTTP {status} {str(body.get('error'))[:40]}")
        else:
            bad("a non-numeric line count was not a clean 400",
                f"{status} {str(body)[:100]}")

        status, body = call("?lines=")
        if status in (200, 400):
            ok("an empty line count does not crash", f"HTTP {status}")
        else:
            bad("an empty line count produced {status}", f"{status} {str(body)[:100]}")

        status, body = call_path("?lines=10", "definitely-no-such-instance")
        if status == 404:
            ok("an unknown instance is 404, like every other per-instance route",
               f"HTTP {status}")
        else:
            bad("an unknown instance did not 404", f"{status} {str(body)[:100]}")
    finally:
        if not args.keep:
            sh(f"{CLI} user del {INSTANCE} --with-instance")
            sh(f"rm -f /var/log/dsh-mu/{INSTANCE}.log")

    code, out = sh(f"{CLI} user list")
    if INSTANCE not in out:
        ok("the throwaway instance is gone")
    else:
        bad("the throwaway instance is still listed")

    line = ""
    code, out = sh(f"{CLI} doctor")
    for candidate in out.splitlines():
        if candidate.startswith("doctor:"):
            line = candidate
    if "0 FAIL" in line and "0 WARN" in line:
        ok("doctor is clean after the drill", line)
    else:
        bad("doctor is not clean after the drill", line or "no doctor line")

    print(f"\n=== LOG BOUNDS: PASS={PASS} FAIL={FAIL} ===")
    print("LOG BOUNDS PASSED" if FAIL == 0 else "LOG BOUNDS FAILED")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
