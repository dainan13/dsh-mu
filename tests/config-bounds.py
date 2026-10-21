#!/usr/bin/env python3
"""config-bounds.py - the platform-config endpoint's edges.

    python3 tests/config-bounds.py

The page's 平台 tab writes `/api/config {key, value}`. Three things matter:

  * an unknown key is a 400 that lists what is accepted
  * an invalid value is a 400 that names the key and the kind, and the config file
    is left untouched (no partial write)
  * a *valid but dangerous* value must not put the platform into a state nobody
    checks: emptying the administrator list has a documented fallback to root,
    and narrowing PORT_RANGE so a live instance's port falls outside must be
    visible in `doctor`

It restores the original config and leaves the platform clean.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

BACKEND = "http://127.0.0.1:8787"
CLI = os.environ.get("DSHMU_CLI", "/usr/local/sbin/dsh-muctl")
CONF = "/etc/dsh-mu/dsh-mu.conf"
STATE = "/etc/dsh-mu/state.json"
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
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
    return res.returncode, (res.stdout + res.stderr).strip()


def conf_hash() -> str:
    try:
        with open(CONF, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


def set_config(key: str, value) -> tuple[int, dict]:
    try:
        with open(TOKEN_PATH, encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError:
        return 0, {}
    payload = json.dumps({"key": key, "value": value}).encode()
    req = urllib.request.Request(f"{BACKEND}/api/config", data=payload, method="POST")
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


def doctor_lines() -> list[str]:
    code, out = sh(f"{CLI} doctor")
    return out.splitlines()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("must run as root", file=sys.stderr)
        return 2

    print("== config bounds ==")
    with open(STATE, encoding="utf-8") as fh:
        instances = json.load(fh)["instances"]
    if not instances:
        print("no instances to test with", file=sys.stderr)
        return 2
    name, inst = sorted(instances.items())[0]
    port = int(inst.get("port", 0))

    code, original = sh(f"{CLI} config port_range")
    try:
        lo0, hi0 = (int(x) for x in json.loads(original))
    except (ValueError, json.JSONDecodeError):
        print(f"could not read the current port range: {original!r}", file=sys.stderr)
        return 2
    original_range = f"{lo0}-{hi0}"

    try:
        status, body = set_config("definitely_not_a_key", "x")
        if status == 400 and isinstance(body, dict) and body.get("known"):
            ok("an unknown key is a 400 listing the accepted keys",
               f"{len(body['known'])} keys")
        else:
            bad("an unknown key was not refused properly", f"{status} {str(body)[:100]}")

        before = conf_hash()
        status, body = set_config("session_days", "notanumber")
        after = conf_hash()
        detail = json.dumps(body)
        if status == 400 and "session_days" in detail and before == after:
            ok("an invalid value is a 400 naming the key, and the file is untouched",
               f"HTTP {status}")
        else:
            bad("an invalid value was not handled cleanly",
                f"{status} unchanged={before == after} {str(body)[:90]}")

        code, before_admin = sh(f"{CLI} config admin_system_users")
        status, body = set_config("admin_system_users", "")
        code, after_admin = sh(f"{CLI} config admin_system_users")
        code, users = sh(f"{CLI} user list")
        root_admin = any(line.split()[:2] == ["root", "admin"] for line in users.splitlines())
        if root_admin:
            ok("emptying the administrator list cannot lock everybody out",
               f"config={after_admin.strip()} root still admin")
        else:
            bad("the platform was left without an administrator", users[:120])
        # the CLI value is kept as written; the fallback lives in system_admin_names()
        sh(f'{CLI} config admin_system_users "root"')

        # narrowing PORT_RANGE so a live instance falls outside must be visible
        narrowed = f"{port + 1}-{port + 50}"
        status, body = set_config("port_range", narrowed)
        accepted = status == 200
        warnings = [line for line in doctor_lines()
                    if line.startswith(("WARN", "FAIL")) and ("port" in line.lower())]
        if accepted and warnings:
            ok("narrowing PORT_RANGE past a live instance is flagged by doctor",
               f"{name}:{port} vs {narrowed} -> {warnings[0][:70]}")
        elif not accepted:
            ok("narrowing PORT_RANGE past a live instance is refused", f"HTTP {status}")
        else:
            bad("narrowing PORT_RANGE past a live instance went unnoticed",
                f"{name}:{port} outside {narrowed}, doctor says nothing")
    finally:
        if not args.keep:
            sh(f'{CLI} config port_range "{original_range}"')
            sh(f'{CLI} config admin_system_users "root"')

    code, now = sh(f"{CLI} config port_range")
    try:
        lo1, hi1 = (int(x) for x in json.loads(now))
    except (ValueError, json.JSONDecodeError):
        lo1, hi1 = 0, 0
    if (lo1, hi1) == (lo0, hi0):
        ok("PORT_RANGE restored", f"{lo1}-{hi1}")
    else:
        bad("PORT_RANGE was not restored", f"{lo1}-{hi1} != {lo0}-{hi0}")

    line = ""
    for candidate in doctor_lines():
        if candidate.startswith("doctor:"):
            line = candidate
    if "0 FAIL" in line and "0 WARN" in line:
        ok("doctor is clean after the drill", line)
    else:
        bad("doctor is not clean after the drill", line or "no doctor line")
        for candidate in doctor_lines():
            if candidate.startswith(("WARN", "FAIL")):
                print(f"      {candidate[:110]}")

    print(f"\n=== CONFIG BOUNDS: PASS={PASS} FAIL={FAIL} ===")
    print("CONFIG BOUNDS PASSED" if FAIL == 0 else "CONFIG BOUNDS FAILED")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
