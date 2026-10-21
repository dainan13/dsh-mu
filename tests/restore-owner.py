#!/usr/bin/env python3
"""restore-owner.py - does a restore leave instance data writable by its owner?

    python3 tests/restore-owner.py

`apply_restore()` unpacks the archive as root. Tar preserves the *recorded*
ownership, so a same-host restore looks fine - but an archive from before a uid
change (or from another host) carries stale owners, and a jailed instance has no
capabilities to fix that itself: it simply cannot write its own home/workspace.

This builds exactly that situation on a throwaway instance with a dedicated uid:

  1. create an instance in `instance_run_uid auto` mode (dedicated host uid)
  2. hand its home/workspace to root, i.e. "the archive carries a stale owner"
  3. back it up (with instances), wipe both volumes, restore
  4. require the volumes to be owned by the instance's uid again, with the
     content intact - the fix is that a restore re-owns the instance volumes
     from the record, so the record (not the archive) decides the owner

Without that step the volumes stay root-owned and the instance loses access to
its own data. It cleans the instance and the account up afterwards.
"""
from __future__ import annotations

import argparse
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


def owner(path: str) -> int:
    try:
        return os.stat(path).st_uid
    except OSError:
        return -1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("must run as root", file=sys.stderr)
        return 2

    name = f"owncheck{os.getpid() % 100000}"
    password = "owner-check-password-1"
    archive = f"/tmp/{name}-backup.tgz"
    print(f"== restore owner: instance={name} ==")

    cfg_before = ""
    code, out = sh(f"{CLI} config instance_run_uid")
    cfg_before = out.strip().strip('"')
    code, out = sh(f"{CLI} config instance_run_uid auto")
    if code != 0:
        bad("could not switch to instance_run_uid auto", out[-100:])
        return 1
    code, out = sh(f"{CLI} user add {name} --password {password}")
    if code != 0:
        sh(f"{CLI} config instance_run_uid {cfg_before}")
        bad("could not create the throwaway instance", out[-100:])
        return 1

    try:
        with open(STATE, encoding="utf-8") as fh:
            inst = json.load(fh)["instances"][name]
        uid = int(inst.get("uid", 0))
        if uid == 0:
            bad("the instance did not get a dedicated uid", f"uid={uid}")
            return 1
        ok("created an instance with a dedicated uid", f"uid={uid}")

        home = f"{INSTANCES}/{name}/home"
        work = f"{INSTANCES}/{name}/workspace"
        os.makedirs(work, exist_ok=True)
        marker = os.urandom(8).hex()
        for path, label in ((home, "home"), (work, "workspace")):
            with open(f"{path}/owner-marker.txt", "w") as fh:
                fh.write(marker)
        ok("wrote the same marker into both volumes")

        # 2. the archive now carries a *stale* owner (root), as it would after a
        # uid change or a restore from another host.
        for path in (home, work):
            code, out = sh(f"chown -R root:root {path}")
            if code != 0:
                bad("could not stage the stale owner", out[-80:])
        if owner(home) == 0 and owner(work) == 0:
            ok("staged a stale owner in both volumes (root)")
        else:
            bad("staging failed", f"home={owner(home)} workspace={owner(work)}")

        code, out = sh(f"{CLI} backup --out {archive} --with-instances")
        if code == 0 and os.path.exists(archive):
            ok("backed up the instance (with volumes)")
        else:
            bad("backup failed", out[-100:])

        code, out = sh(f"{CLI} instance stop {name}")
        code, out = sh(f"rm -rf {home} {work}")
        if not os.path.exists(home) and not os.path.exists(work):
            ok("wiped both volumes (simulating a lost machine)")
        else:
            bad("could not wipe the volumes")

        code, out = sh(f"{CLI} restore {archive} --force")
        if code == 0:
            ok("restore completed", out.splitlines()[-1][:60] if out else "")
        else:
            bad("restore failed", out[-160:])

        for path, label in ((home, "home"), (work, "workspace")):
            if not os.path.isdir(path):
                bad(f"the {label} volume did not come back")
                continue
            got = owner(path)
            if got == uid:
                ok(f"the restored {label} volume is owned by the instance uid", f"uid={got}")
            else:
                bad(f"the restored {label} volume is owned by uid {got}, not {uid}")
            marker_file = f"{path}/owner-marker.txt"
            try:
                with open(marker_file) as fh:
                    if fh.read().strip() == marker:
                        ok(f"the {label} content came back intact")
                    else:
                        bad(f"the {label} marker content differs")
            except OSError as exc:
                bad(f"could not read the {label} marker", str(exc)[:60])

        # the instance must be able to write there again (it runs with no caps)
        code, out = sh(f"{CLI} instance start {name}")
        time.sleep(3)
        code, out = sh(f"{CLI} instance exec {name} -- "
                       "'touch /home/dsh/owner-write-test; "
                       "touch /home/dsh/workspace/owner-write-test; echo WROTE' 2>&1")
        if "WROTE" in out:
            ok("the instance can write to both restored volumes")
        else:
            bad("the instance cannot write to its restored volumes", out[-120:])
    finally:
        if not args.keep:
            sh(f"{CLI} instance stop {name}")
            sh(f"{CLI} user del {name} --with-instance")
            sh(f"rm -f {archive}")
        sh(f"{CLI} config instance_run_uid {cfg_before}")

    code, out = sh(f"{CLI} user list")
    if name not in out:
        ok("the throwaway account and instance are gone")
    else:
        bad("the throwaway account is still listed")
    code, out = sh(f"{CLI} config instance_run_uid")
    if out.strip().strip('"') == cfg_before:
        ok("instance_run_uid restored", out.strip())

    line = ""
    code, out = sh(f"{CLI} doctor")
    for candidate in out.splitlines():
        if candidate.startswith("doctor:"):
            line = candidate
    if "0 FAIL" in line and "0 WARN" in line:
        ok("doctor is clean after the drill", line)
    else:
        bad("doctor is not clean after the drill", line or "no doctor line")

    print(f"\n=== RESTORE OWNER: PASS={PASS} FAIL={FAIL} ===")
    print("RESTORE OWNER PASSED" if FAIL == 0 else "RESTORE OWNER FAILED")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
