#!/usr/bin/env python3
"""name-key-bounds.py - the names and keys an operator can type.

    python3 tests/name-key-bounds.py

Account names become filesystem paths (instance dirs, log files, service names)
and API keys end up inside a jailed instance, so both have to be validated - and
validated *the same way at every entrance*:

  * a name must match [a-z][a-z0-9_-]{0,31}: `../escape`, `a/b`, `..`, `A`, spaces
    and over-long names are refused, with no trace left behind (no user record,
    no instance directory)
  * an existing administrator's name cannot be taken over as a platform account
    (that would create an instance whose record is then overwritten by the admin)
  * an API key must be at least 8 characters *whether it is set from the CLI or
    from the page* - the two paths disagreed, which is what this pins

Everything it creates is removed again.
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
STATE = os.environ.get("DSHMU_STATE", "/etc/dsh-mu/state.json")
INSTANCES = "/var/lib/dsh-mu/instances"
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


def users() -> set[str]:
    try:
        with open(STATE, encoding="utf-8") as fh:
            return set(json.load(fh).get("users") or {})
    except (OSError, json.JSONDecodeError):
        return set()


def set_key(name: str, key: str) -> tuple[int, dict]:
    with open(TOKEN_PATH, encoding="utf-8") as fh:
        token = fh.read().strip()
    req = urllib.request.Request(f"{BACKEND}/api/instances/{name}/key", method="POST",
                                 data=json.dumps({"api_key": key}).encode())
    req.add_header("X-DSH-MU-Admin", token)
    req.add_header("Content-Type", "application/json")
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("must run as root", file=sys.stderr)
        return 2

    print("== name and key bounds ==")
    before = users()
    before_dirs = set(os.listdir(INSTANCES))

    bad_names = ["../escape", "a/b", ".", "..", "-lead", "a b", "UPPER", "a.b",
                 "9lead", "x" * 33, ""]
    leaked = []
    for candidate in bad_names:
        code, out = sh(f"{CLI} user add '{candidate}' --password name-bounds-password-1 "
                       "--instance none")
        # A leading dash never reaches the platform (argparse rejects it), which is
        # just as safe; everything else must fail the name rule itself.
        if code == 0:
            leaked.append(f"{candidate} (accepted!)")
        elif candidate and not candidate.startswith("-") and "username must match" not in out:
            leaked.append(f"{candidate} (wrong reason: {out.splitlines()[-1][:40]})")
    if not leaked:
        ok(f"all {len(bad_names)} invalid names are refused",
           "e.g. ../escape, a/b, ., .., A, spaces, 33 chars, leading dash")
    else:
        bad("some invalid names were not refused", ", ".join(leaked[:5]))

    if users() == before:
        ok("refused names left no user records behind")
    else:
        bad("a refused name still changed the state", str(sorted(users() - before))[:80])

    after_dirs = set(os.listdir(INSTANCES))
    if after_dirs == before_dirs:
        ok("refused names created no instance directories", f"{len(after_dirs)} dirs")
    else:
        bad("refused names left directories behind", str(sorted(after_dirs - before_dirs))[:80])

    # an administrator's name cannot be taken over
    code, out = sh(f"{CLI} user add root --password name-bounds-password-1 --instance auto")
    with open(STATE, encoding="utf-8") as fh:
        state = json.load(fh)
    root = state["users"].get("root") or {}
    if code != 0 and "already exists" in out and root.get("role") == "admin" \
            and not root.get("instance"):
        ok("an administrator's name cannot be taken over (no instance created)",
           f"role={root.get('role')} instance={root.get('instance')!r}")
    else:
        bad("adding a user named after an administrator did something odd",
            f"exit={code} role={root.get('role')} instance={root.get('instance')!r}")

    # keys: one rule everywhere
    name = f"keybounds{os.getpid() % 100000}"
    code, out = sh(f"{CLI} user add {name} --password name-bounds-password-1")
    if code != 0:
        bad("could not create the throwaway instance", out[-100:])
        print(f"\n=== NAME KEY BOUNDS: PASS={PASS} FAIL={FAIL} ===")
        return 1
    try:
        code, out = sh(f"{CLI} instance api-key {name} x")
        if code != 0 and "too short" in out:
            ok("the CLI refuses a 1-character API key", out.splitlines()[-1][:60])
        else:
            bad("the CLI accepted a 1-character API key", out[-90:])

        code, out = sh(f"{CLI} instance api-key {name} 1234567")
        if code != 0:
            ok("the CLI refuses a 7-character API key")
        else:
            bad("the CLI accepted a 7-character API key")

        code, out = sh(f"{CLI} instance api-key {name} 12345678")
        if code == 0:
            ok("the CLI accepts an 8-character API key (the boundary)")
        else:
            bad("the CLI refused a valid 8-character key", out[-90:])

        status, body = set_key(name, "x")
        if status == 400:
            ok("the page's API refuses a 1-character key too", f"HTTP {status}")
        else:
            bad("the page's API accepted a 1-character key", f"{status} {str(body)[:80]}")

        status, body = set_key(name, "12345678")
        if status == 200:
            ok("the page's API accepts an 8-character key (same boundary)")
        else:
            bad("the page's API refused a valid key", f"{status} {str(body)[:80]}")

        code, out = sh(f"{CLI} instance api-key {name} --remove")
        if code == 0:
            ok("removing the key is still allowed")
        else:
            bad("could not remove the key", out[-90:])
    finally:
        if not args.keep:
            sh(f"{CLI} user del {name} --with-instance")

    if name not in users():
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

    print(f"\n=== NAME KEY BOUNDS: PASS={PASS} FAIL={FAIL} ===")
    print("NAME KEY BOUNDS PASSED" if FAIL == 0 else "NAME KEY BOUNDS FAILED")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
