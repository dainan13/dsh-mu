#!/usr/bin/env python3
"""state-lock.py - is state.json actually protected against concurrent writers?

    python3 tests/state-lock.py

Two things are worth proving, and the second is the one that matters:

  1. concurrent mutating commands do not corrupt state.json (it stays valid JSON
     and keeps every account it should)
  2. the lock really *blocks* - a writer that arrives while another holds the
     lock waits instead of racing (a lock nobody waits on is decoration)

Side effects (deliberately small, see the round-42 lesson about shared budgets):
it creates one throwaway account, writes state a few times and holds the state
lock for ~3 seconds. It does not touch logins, ports, cgroups or the audit
budget, so it is safe to run anywhere - but it does hold the platform lock, so do
not run it concurrently with other mutating layers.
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
# NB: not DSHMU_LIB_DIR - tests/lib.sh already uses that name for the platform
# library root (/usr/local/lib/dsh-mu). Reusing it here made the helper process
# import from the wrong directory and die with a traceback nobody could see.
LIB = os.environ.get("DSHMU_PYTHON_DIR", "/usr/local/lib/dsh-mu/python")
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
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180)
    return res.returncode, (res.stdout + res.stderr).strip()


def state_is_valid() -> tuple[bool, str]:
    try:
        with open(STATE, encoding="utf-8") as fh:
            data = json.load(fh)
        return isinstance(data.get("users"), dict), f"{len(data.get('users', {}))} users"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--writers", type=int, default=8)
    ap.add_argument("--hold", type=float, default=3.0)
    args = ap.parse_args()

    user = f"lockcheck{os.getpid() % 100000}"
    print(f"== state lock: account={user}, {args.writers} concurrent writers ==")

    code, out = sh(f"{CLI} user add {user} --password lock-check-password-1 --instance none")
    if code != 0:
        bad("could not create the throwaway account", out[-100:])
        print(f"\n=== STATE LOCK: PASS={PASS} FAIL={FAIL} ===")
        return 1
    ok("created the throwaway account")

    try:
        before_valid, before_note = state_is_valid()
        if before_valid:
            ok("state.json is valid JSON before the test", before_note)
        else:
            bad("state.json was already broken", before_note)

        # ---------------------------------------------------------- 1. contention
        # Alternate disable/enable on the same account: every command rewrites the
        # whole state file, so unsynchronised writers would clobber each other.
        procs = []
        for i in range(args.writers):
            action = "disable" if i % 2 == 0 else "enable"
            procs.append(subprocess.Popen(f"{CLI} user {action} {user}",
                                          shell=True, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, text=True))
        codes = []
        for proc in procs:
            proc.wait(timeout=180)
            codes.append(proc.returncode)
        if all(c == 0 for c in codes):
            ok(f"all {args.writers} concurrent writers succeeded", f"exit codes {set(codes)}")
        else:
            bad(f"some concurrent writers failed", f"exit codes {sorted(set(codes))}")

        after_valid, after_note = state_is_valid()
        if after_valid:
            ok("state.json is still valid JSON after the contention", after_note)
        else:
            bad("state.json was corrupted by concurrent writers", after_note)

        code, listed = sh(f"{CLI} user list")
        if code == 0 and user in listed:
            ok("the account survived the contention (no lost update of the whole file)")
        else:
            bad("the account disappeared from state.json", listed[-100:])

        # -------------------------------------------------------------- 2. blocking
        # Hold the platform lock in a child process, then time a CLI write: if the
        # lock is real, the write cannot finish before the hold ends.
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time; sys.path.insert(0, sys.argv[1]); import dshmuctl as mu;"
             "lock = mu.state_lock(); lock.__enter__(); print('held', flush=True);"
             "time.sleep(float(sys.argv[2])); lock.__exit__(None, None, None)",
             LIB, str(args.hold)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        line = (holder.stdout.readline() or "").strip() if holder.stdout else ""
        if line != "held":
            # Show *why* it failed instead of swallowing the traceback.
            rest = (holder.stdout.read() or "").strip() if holder.stdout else ""
            bad("could not take the state lock in a helper process",
                (line + " " + rest).strip()[:160] or "no output")
        else:
            ok("a helper process is holding the platform lock")
            start = time.monotonic()
            code, out = sh(f"{CLI} user enable {user}")
            elapsed = time.monotonic() - start
            holder.wait(timeout=60)
            if code != 0:
                bad("the blocked writer failed instead of waiting", out[-100:])
            elif elapsed >= args.hold * 0.6:
                ok("a writer arriving during the hold waited for it",
                   f"took {elapsed:.1f}s while the lock was held {args.hold:.0f}s")
            else:
                bad("the writer did not wait for the lock",
                   f"finished in {elapsed:.2f}s although the lock was held {args.hold:.0f}s")
    finally:
        sh(f"{CLI} user del {user}")

    code, listed = sh(f"{CLI} user list")
    if user not in listed:
        ok("the throwaway account is gone")
    else:
        bad("the throwaway account is still listed")
    valid, note = state_is_valid()
    if valid:
        ok("state.json is valid at the end", note)
    else:
        bad("state.json is broken at the end", note)

    print(f"\n=== STATE LOCK: PASS={PASS} FAIL={FAIL} ===")
    print("STATE LOCK PASSED" if FAIL == 0 else "STATE LOCK FAILED")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
