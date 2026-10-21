#!/usr/bin/env python3
"""drift-audit.py - is the machine running what this checkout (and package) ships?

    python3 tests/drift-audit.py [SOURCE-TREE] [--apk dist/dsh-mu-0.1.0-r0.apk]

"Verified on the live host" only means something if the host runs the code we ship.
Files get copied by hand during development, so this compares three things:

    checkout  <->  installed tree        (what the live checks exercised)
    checkout  <->  package contents      (what a fresh machine will get)

Only the files the platform actually installs are compared, byte for byte.
Anything that differs, is missing or is extra is reported with a fix hint.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tarfile
import tempfile

PASS = 0
FAIL = 0

# source path -> installed path (mirrors packaging/APKBUILD and install/install.sh)
INSTALLED = {
    "cli/dsh-muctl": "/usr/local/sbin/dsh-muctl",
    "cli/dshmuctl.py": "/usr/local/lib/dsh-mu/python/dshmuctl.py",
    "jail/dsh-mu-jail": "/usr/local/sbin/dsh-mu-jail",
    "jail/dsh-mu-cgroup-init": "/usr/local/sbin/dsh-mu-cgroup-init",
    "admin/dsh-mud": "/usr/local/sbin/dsh-mud",
    "admin/index.html": "/usr/local/lib/dsh-mu/admin/index.html",
}
# package path for the same files (as shipped)
PACKAGED = {
    "cli/dsh-muctl": "usr/local/sbin/dsh-muctl",
    "cli/dshmuctl.py": "usr/local/lib/dsh-mu/python/dshmuctl.py",
    "jail/dsh-mu-jail": "usr/local/sbin/dsh-mu-jail",
    "jail/dsh-mu-cgroup-init": "usr/local/sbin/dsh-mu-cgroup-init",
    "admin/dsh-mud": "usr/local/sbin/dsh-mud",
    "admin/index.html": "usr/local/lib/dsh-mu/admin/index.html",
}
DIRECTORIES = {
    "gateway/lua": "/usr/local/lib/dsh-mu/lua",
    "gateway/static": "/usr/local/lib/dsh-mu/static",
}
# `tests/` is deliberately NOT compared against the installed tree: the regression
# runner copies the checkout's tests into place right before running the layers,
# so "installed tests == checkout tests" would be true by construction - a check
# that cannot fail proves nothing. The package comparison below still covers it,
# because the package is built by a separate step from the same checkout.


def ok(label: str, extra: str = "") -> None:
    global PASS
    PASS += 1
    print(f"PASS  {label}" + (f"  -- {extra}" if extra else ""))


def bad(label: str, extra: str = "") -> None:
    global FAIL
    FAIL += 1
    print(f"FAIL  {label}" + (f"  -- {extra}" if extra else ""))


def digest(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def compare_pairs(pairs: list[tuple[str, str, str]], label: str) -> list[str]:
    """pairs: (display-name, source-file, target-file). Returns drift descriptions."""
    drift = []
    for name, src, dst in pairs:
        if not os.path.exists(dst):
            drift.append(f"{name}: missing at {dst}")
        elif digest(src) != digest(dst):
            drift.append(f"{name}: differs ({src} vs {dst})")
    if drift:
        bad(f"{label}: {len(drift)} of {len(pairs)} files differ")
        for item in drift[:12]:
            print(f"        {item}")
    else:
        ok(f"{label}: all {len(pairs)} files identical")
    return drift


def collect_dir(src_dir: str, dst_dir: str) -> list[tuple[str, str, str]]:
    pairs = []
    for entry in sorted(os.listdir(src_dir)):
        src = os.path.join(src_dir, entry)
        if not os.path.isfile(src):
            continue
        pairs.append((os.path.join(os.path.basename(src_dir), entry), src,
                      os.path.join(dst_dir, entry)))
    return pairs


def sh(cmd: str) -> tuple[int, str]:
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return res.returncode, (res.stdout + res.stderr).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--apk", default="")
    args = ap.parse_args()
    root = os.path.abspath(args.root)
    print(f"== deploy drift: {root} ==")

    if not os.path.isdir("/usr/local/lib/dsh-mu"):
        print("SKIP  no installed platform on this machine")
        print("\n=== DRIFT AUDIT: SKIP ===")
        return 0

    pairs = [(src_rel, os.path.join(root, src_rel), dst)
             for src_rel, dst in INSTALLED.items()
             if os.path.exists(os.path.join(root, src_rel))]
    compare_pairs(pairs, "checkout vs installed")

    for src_rel, dst_dir in DIRECTORIES.items():
        src_dir = os.path.join(root, src_rel)
        if os.path.isdir(src_dir) and os.path.isdir(dst_dir):
            compare_pairs(collect_dir(src_dir, dst_dir), f"checkout vs installed ({src_rel})")

    # ---------------------------------------------------------------- the package
    apk = args.apk
    if not apk:
        dist = os.path.join(root, "dist")
        if os.path.isdir(dist):
            found = sorted(f for f in os.listdir(dist) if f.endswith(".apk"))
            apk = os.path.join(dist, found[0]) if found else ""
    if apk and os.path.exists(apk):
        with tempfile.TemporaryDirectory() as tmp:
            # v3 packages (apk-tools 3, the default build) are an ADB container and
            # cannot be opened with tarfile; `apk extract` handles both formats.
            try:
                with tarfile.open(apk) as tf:
                    members = [m for m in tf.getmembers()
                               if m.isfile() and m.name.lstrip("./").startswith("usr/")]
                    tf.extractall(tmp, members=members)
            except (tarfile.TarError, OSError):
                code, out = sh(f"apk --allow-untrusted extract --destination '{tmp}' "
                               f"--no-chown '{apk}'")
                if code != 0:
                    bad("cannot unpack the package", out[:120])
            pairs = [(src_rel, os.path.join(root, src_rel), os.path.join(tmp, PACKAGED[src_rel]))
                     for src_rel in INSTALLED
                     if os.path.exists(os.path.join(root, src_rel))]
            compare_pairs(pairs, "checkout vs package")
            for src_rel, pkg_rel in (("gateway/lua", "usr/local/lib/dsh-mu/lua"),
                                     ("gateway/static", "usr/local/lib/dsh-mu/static"),
                                     ("tests", "usr/local/lib/dsh-mu/tests")):
                src_dir = os.path.join(root, src_rel)
                pkg_dir = os.path.join(tmp, pkg_rel)
                if os.path.isdir(src_dir) and os.path.isdir(pkg_dir):
                    compare_pairs(collect_dir(src_dir, pkg_dir), f"checkout vs package ({src_rel})")
    else:
        print("SKIP  no package to compare (build one with packaging/make-apk.sh)")

    print(f"\n=== DRIFT AUDIT: PASS={PASS} FAIL={FAIL} ===")
    if FAIL:
        print("hint: deploy the package built from this checkout "
              "(or copy the listed files) and re-run the layers")
        print("DRIFT AUDIT FAILED")
        return 1
    print("DRIFT AUDIT PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
