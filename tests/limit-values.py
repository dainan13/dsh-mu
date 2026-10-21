#!/usr/bin/env python3
"""limit-values.py - what `instance limits` accepts, and what that does to the cgroup.

    python3 tests/limit-values.py

Memory and CPU both have an "unlimited" form, and `0` is the documented way to ask
for it. The risky direction is the other one: a value that *looks* like a limit but
silently becomes "unlimited" removes a protection instead of adding one. So:

  * `0`, `max`, `none` -> unlimited (written as `max` into the cgroup)
  * a negative number  -> refused, because `-1` is a typo, not a policy
  * a non-number       -> refused, naming the flag
  * explicit units (`2G`, `1.5` cores) -> the right byte/quota values, live

It works on a throwaway instance, checks the live cgroup values, and restores the
instance's original limits before removing it.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

CLI = os.environ.get("DSHMU_CLI", "/usr/local/sbin/dsh-muctl")
STATE = os.environ.get("DSHMU_STATE", "/etc/dsh-mu/state.json")
CGROUP = "/sys/fs/cgroup/dsh-mu"
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


def cg(name: str, key: str) -> str:
    try:
        with open(f"{CGROUP}/{name}/{key}", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def limits_of(name: str) -> dict:
    try:
        with open(STATE, encoding="utf-8") as fh:
            inst = json.load(fh)["instances"][name]
        return {k: inst.get(k) for k in ("cpu_max", "mem_max", "pids_max")}
    except (OSError, json.JSONDecodeError, KeyError):
        return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("must run as root", file=sys.stderr)
        return 2

    name = f"limcheck{os.getpid() % 100000}"
    print(f"== limit values: instance={name} ==")

    code, out = sh(f"{CLI} user add {name} --password limit-check-password-1")
    if code != 0:
        bad("could not create the throwaway instance", out[-100:])
        return 1

    try:
        code, out = sh(f"{CLI} instance start {name}")
        if cg(name, "memory.max"):
            ok("the instance is running with its own cgroup")
        else:
            bad("no cgroup for the instance", cg(name, "memory.max") or "missing")
        original = limits_of(name)

        code, out = sh(f"{CLI} instance limits {name} --mem-mb 0")
        if code == 0 and limits_of(name).get("mem_max") == "max" and cg(name, "memory.max") == "max":
            ok("--mem-mb 0 means unlimited, and the cgroup says `max`")
        else:
            bad("--mem 0 did not become unlimited",
                f"record={limits_of(name).get('mem_max')} cgroup={cg(name, 'memory.max')!r}")

        code, out = sh(f"{CLI} instance limits {name} --cpu-cores 0")
        if code == 0 and limits_of(name).get("cpu_max") == "max" \
                and cg(name, "cpu.max").startswith("max"):
            ok("--cpu-cores 0 means unlimited, and the cgroup says `max`", cg(name, "cpu.max"))
        else:
            bad("--cpu 0 did not become unlimited",
                f"record={limits_of(name).get('cpu_max')} cgroup={cg(name, 'cpu.max')!r}")

        code, out = sh(f"{CLI} instance limits {name} --mem-mb -1")
        if code != 0 and "mem" in out.lower():
            ok("a negative memory limit is refused, not silently unlimited",
               out.splitlines()[-1][:70])
        else:
            bad("a negative memory limit was accepted",
                f"record={limits_of(name).get('mem_max')} (silently unlimited?)")

        code, out = sh(f"{CLI} instance limits {name} --cpu-cores -1")
        if code != 0 and "cpu" in out.lower():
            ok("a negative CPU limit is refused", out.splitlines()[-1][:70])
        else:
            bad("a negative CPU limit was accepted",
                f"record={limits_of(name).get('cpu_max')} (silently unlimited?)")

        code, out = sh(f"{CLI} instance limits {name} --mem-mb abc")
        if code != 0:
            ok("a non-numeric memory limit is refused", out.splitlines()[-1][:60])
        else:
            bad("a non-numeric memory limit was accepted")

        code, out = sh(f"{CLI} instance limits {name} --mem-mb 2G")
        if code == 0 and cg(name, "memory.max") == str(2 * 1024 ** 3):
            ok("an explicit unit lands as the right byte count in the cgroup",
               f"2G -> {cg(name, 'memory.max')}")
        else:
            bad("2G did not reach the cgroup", f"cgroup={cg(name, 'memory.max')!r}")

        code, out = sh(f"{CLI} instance limits {name} --cpu-cores 1.5")
        if code == 0 and cg(name, "cpu.max").startswith("150000"):
            ok("fractional cores become the right cpu.max quota", cg(name, "cpu.max"))
        else:
            bad("1.5 cores did not reach the cgroup", f"cgroup={cg(name, 'cpu.max')!r}")

        code, out = sh(f"{CLI} instance limits {name} --pids-num 0")
        pids_record = limits_of(name).get("pids_max")
        if code == 0 and pids_record == "max" and cg(name, "pids.max") == "max":
            ok("--pids-num 0 means unlimited", f"record={pids_record} cgroup={cg(name, 'pids.max')}")
        else:
            bad("--pids 0 misbehaved", f"record={pids_record} cgroup={cg(name, 'pids.max')!r}")

        raw_zero = []
        for flag in ("--mem", "--pids"):
            code, out = sh(f"{CLI} instance limits {name} {flag} 0")
            if code == 0:
                raw_zero.append(flag)
        if not raw_zero:
            ok("the raw cgroup flags refuse 0 (it would allow nothing, not everything)")
        else:
            bad("a raw 0 limit was accepted and written to the cgroup", ", ".join(raw_zero))

        code, out = sh(f"{CLI} instance limits {name} --mem max")
        if code == 0 and cg(name, "memory.max") == "max":
            ok("the raw form still accepts `max`", cg(name, "memory.max"))
        else:
            bad("the raw `max` form stopped working", f"cgroup={cg(name, 'memory.max')!r}")

        # a very large but honest limit must be accepted without breaking anything
        code, out = sh(f"{CLI} instance limits {name} --mem-mb 1048576")
        alive = cg(name, "memory.max") == str(1024 ** 4)
        if code == 0 and alive:
            ok("a very large limit is accepted and applied", f"cgroup={cg(name, 'memory.max')}")
        else:
            bad("a very large limit was mishandled", f"cgroup={cg(name, 'memory.max')!r}")
        restored = {}
        for flag, key in (("--cpu", "cpu_max"), ("--mem", "mem_max"), ("--pids", "pids_max")):
            value = (original or {}).get(key)
            if value:
                sh(f'{CLI} instance limits {name} {flag} "{value}"')
                restored[key] = value
        after = limits_of(name)
        if all(str(after.get(k)) == str(v) for k, v in restored.items()):
            ok("the instance's original limits were restored and verified",
               ", ".join(f"{k}={v}" for k, v in restored.items()) or "none set")
        else:
            bad("the original limits were not restored",
                f"now={after} wanted={restored}")
    finally:
        if not args.keep:
            sh(f"{CLI} instance stop {name}")
            sh(f"{CLI} user del {name} --with-instance")

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

    print(f"\n=== LIMIT VALUES: PASS={PASS} FAIL={FAIL} ===")
    print("LIMIT VALUES PASSED" if FAIL == 0 else "LIMIT VALUES FAILED")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
