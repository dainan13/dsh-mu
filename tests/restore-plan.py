#!/usr/bin/env python3
"""restore-plan.py - the report an operator reads *before* overwriting live data.

    python3 tests/restore-plan.py

`dsh-muctl restore <archive> --dry-run` is the only thing standing between an
operator and clobbering a live platform, so it must:

  * change nothing at all
  * name the instance volumes that will be written (not just "instance files: yes"),
    because with `--with-instances` a restore overwrites live user data
  * say which of them already exist live (i.e. will be overwritten)
  * warn when the archive is older than the live platform (restoring a stale
    backup is the classic way to lose a day of work)

It builds a small archive, then changes the live platform afterwards so the
archive is provably older, and checks the report - and that nothing moved.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

CLI = os.environ.get("DSHMU_CLI", "/usr/local/sbin/dsh-muctl")
STATE = os.environ.get("DSHMU_STATE", "/etc/dsh-mu/state.json")
INSTANCES = "/var/lib/dsh-mu/instances"
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
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=600)
    return res.returncode, (res.stdout + res.stderr).strip()


def digest_tree(root: str) -> str:
    h = hashlib.sha256()
    for base, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(base, name)
            h.update(os.path.relpath(path, root).encode())
            try:
                with open(path, "rb") as fh:
                    h.update(fh.read())
            except OSError:
                pass
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("must run as root", file=sys.stderr)
        return 2

    name = f"plancheck{os.getpid() % 100000}"
    password = "plan-check-password-1"
    archive = f"/tmp/{name}.tgz"
    print(f"== restore plan: instance={name} ==")

    code, out = sh(f"{CLI} user add {name} --password {password}")
    if code != 0:
        bad("could not create the throwaway instance", out[-100:])
        return 1
    ok("created a throwaway instance")

    try:
        work = f"{INSTANCES}/{name}/workspace"
        home = f"{INSTANCES}/{name}/home"
        os.makedirs(work, exist_ok=True)
        with open(f"{work}/before-backup.txt", "w") as fh:
            fh.write("archived\n")
        code, out = sh(f"{CLI} backup --out {archive} --with-instances")
        if code != 0 or not os.path.exists(archive):
            bad("backup failed", out[-120:])
            return 1
        ok("took a backup that includes the instance volumes")

        # make the live platform newer than the archive: a file the archive does
        # not have, and a state write
        time.sleep(1.2)
        with open(f"{work}/after-backup.txt", "w") as fh:
            fh.write("live-only\n")
        sh(f"{CLI} user enable {name}")
        ok("wrote newer live data after the backup")

        plan_before = hashlib.sha256(open(STATE, "rb").read()).hexdigest()
        work_before = digest_tree(work)

        code, out = sh(f"{CLI} restore {archive} --dry-run")
        if code == 0:
            ok("the dry run succeeds")
        else:
            bad("the dry run failed", out[-160:])

        if hashlib.sha256(open(STATE, "rb").read()).hexdigest() == plan_before:
            ok("the dry run did not touch state.json")
        else:
            bad("the dry run modified state.json")
        if digest_tree(work) == work_before:
            ok("the dry run did not touch the instance workspace")
        else:
            bad("the dry run modified the instance workspace")

        # it must name the volumes it would write, not just say "included: True"
        named = [p for p in (f"instances/{name}/home", f"instances/{name}/workspace")
                 if p in out]
        if len(named) == 2:
            ok("the report names both instance volumes", ", ".join(named))
        else:
            bad("the report does not name the instance volumes",
                "found: " + (", ".join(named) or "none"))

        if "overwrit" in out.lower():
            ok("the report says the existing volumes would be overwritten")
        else:
            bad("the report does not warn about overwriting live data")

        if "older" in out.lower():
            ok("the report warns that the archive is older than the live platform")
        else:
            bad("the report does not warn about a stale archive")

        code, out = sh(f"{CLI} restore {archive} --dry-run --json")
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            payload = {}
        volumes = payload.get("archive_volumes") or []
        overwrites = payload.get("overwrites") or []
        if code == 0 and len(volumes) >= 2 and overwrites:
            ok("the JSON plan exposes the same facts",
               f"{len(volumes)} volumes, {len(overwrites)} would be overwritten")
        else:
            bad("the JSON plan lacks archive_volumes/overwrites",
                f"keys={sorted(payload)[:8]}")
    finally:
        if not args.keep:
            sh(f"{CLI} user del {name} --with-instance")
            sh(f"rm -f {archive}")

    code, out = sh(f"{CLI} user list")
    if name not in out:
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

    print(f"\n=== RESTORE PLAN: PASS={PASS} FAIL={FAIL} ===")
    print("RESTORE PLAN PASSED" if FAIL == 0 else "RESTORE PLAN FAILED")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
