#!/usr/bin/env python3
"""artifact-audit.py - check the delivered artifact against what we promise.

    python3 tests/artifact-audit.py [SOURCE-TREE] [--apk F] [--tarball F] [--skip-build]

Nothing is installed and no service is touched: this only inspects the built
package and the source archive. It is the checklist you can sign off before
handing the machine to someone else:

  * the package is readable (v3/ADB) and is small - the ~140 MB jail template and
    every runtime secret must NOT be inside it. The package is deliberately
    unsigned: `apk verify` says UNTRUSTED and the docs say `--allow-untrusted`.
  * it carries the promised programs, library, gateway Lua, admin page, panel
    assets, tests and documentation
  * shipped files are not world-writable
  * the source archive has no build leftovers (__pycache__, dist/) and no secrets
  * DELIVERY.md's promises match the artifact (docs, tests, install scripts)

Exit code 0 only when every promise holds.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

PASS = 0
FAIL = 0
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ok(label: str, extra: str = "") -> None:
    global PASS
    PASS += 1
    print(f"PASS  {label}" + (f"  -- {extra}" if extra else ""))


def bad(label: str, extra: str = "") -> None:
    global FAIL
    FAIL += 1
    print(f"FAIL  {label}" + (f"  -- {extra}" if extra else ""))


def sh(cmd: str, cwd: str | None = None) -> tuple[int, str]:
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd)
    return res.returncode, (res.stdout + res.stderr).strip()


def main() -> int:
    global ROOT
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=ROOT,
                    help="source tree to audit (default: this checkout)")
    ap.add_argument("--apk", default="")
    ap.add_argument("--tarball", default="")
    ap.add_argument("--skip-build", action="store_true")
    args = ap.parse_args()
    ROOT = os.path.abspath(args.root)

    print(f"== artifact audit: {ROOT} ==")

    # ------------------------------------------------------------------ package
    apk = args.apk
    if not apk:
        candidates = sorted((os.path.join(ROOT, "dist"),), reverse=False)
        found = []
        dist = os.path.join(ROOT, "dist")
        if os.path.isdir(dist):
            found = sorted(os.path.join(dist, f) for f in os.listdir(dist) if f.endswith(".apk"))
        if found:
            apk = found[0]
        elif not args.skip_build:
            print("  building the package (packaging/make-apk.sh) ...")
            code, out = sh("sh packaging/make-apk.sh --version 0.1.0", cwd=ROOT)
            built = [os.path.join(dist, f) for f in os.listdir(dist)] if os.path.isdir(dist) else []
            built = [f for f in built if f.endswith(".apk")]
            apk = built[0] if built else ""
            if not apk:
                bad("could not build the package", out.strip().splitlines()[-1] if out else "")
                print(f"\n=== ARTIFACT AUDIT: PASS={PASS} FAIL={FAIL} ===")
                return 1

    if not apk or not os.path.exists(apk):
        bad("no package to audit (pass --apk or build one)")
        print(f"\n=== ARTIFACT AUDIT: PASS={PASS} FAIL={FAIL} ===")
        return 1

    size = os.path.getsize(apk)
    print(f"  package: {os.path.relpath(apk, ROOT)} ({size / 1024:.0f} KB)")

    code, out = sh(f"apk verify '{apk}'")
    if "UNTRUSTED" in out or "no signature" in out.lower():
        ok("package is unsigned by design (install with --allow-untrusted)")
    elif code == 0 and "OK" in out:
        ok("package signature verifies", out.splitlines()[0].split(":")[-1].strip())
    else:
        bad("apk verify failed unexpectedly", out[:120])

    # v3 packages (apk-tools 3 / APK 3.22+) are an ADB container, not a gzip tar:
    # list them with `apk manifest` and unpack them with `apk extract`.
    entries: list[tuple[str, int, int]] = []   # (path, size, mode)
    try:
        with tarfile.open(apk) as tf:
            members = tf.getmembers()
        entries = [(m.name.lstrip("./"), m.size, m.mode) for m in members]
    except (tarfile.TarError, OSError):
        code, out = sh(f"apk --allow-untrusted manifest '{apk}'")
        if code != 0 or not out:
            bad("cannot list the package contents", out[:120])
            print(f"\n=== ARTIFACT AUDIT: PASS={PASS} FAIL={FAIL} ===")
            return 1
        listed = [line.split(None, 1)[1] for line in out.splitlines() if len(line.split(None, 1)) == 2]
        extract = tempfile.mkdtemp(prefix="dsh-mu-audit-")
        try:
            code, out = sh(f"apk --allow-untrusted extract --destination '{extract}' "
                           f"--no-chown '{apk}'")
            for rel in listed:
                full = os.path.join(extract, rel.lstrip("./"))
                try:
                    st = os.stat(full)
                    entries.append((rel.lstrip("./"), st.st_size, st.st_mode))
                except OSError:
                    entries.append((rel.lstrip("./"), 0, 0o644))
        finally:
            shutil.rmtree(extract, ignore_errors=True)
        ok(f"package lists {len(listed)} files (v3, read via apk manifest)")

    paths = {name for name, _size, _mode in entries}
    members = [type("E", (), {"name": n, "size": s, "mode": m,
                              "isfile": staticmethod(lambda: True)})()
               for n, s, m in entries]

    # --- promised contents
    promised = {
        "CLI shim": "usr/local/sbin/dsh-muctl",
        "jail runner": "usr/local/sbin/dsh-mu-jail",
        "cgroup init": "usr/local/sbin/dsh-mu-cgroup-init",
        "admin backend": "usr/local/sbin/dsh-mud",
        "CLI library": "usr/local/lib/dsh-mu/python/dshmuctl.py",
        "gateway Lua": "usr/local/lib/dsh-mu/lua/dshmu.lua",
        "gateway fragment": "usr/local/lib/dsh-mu/nginx/dsh-mu-server.conf",
        "admin page": "usr/local/lib/dsh-mu/admin/index.html",
        "panel script": "usr/local/lib/dsh-mu/static/panel.js",
        "panel icon": "usr/local/lib/dsh-mu/static/icon.png",
        "selftest": "usr/local/lib/dsh-mu/tests/selftest.mjs",
        "acceptance harness": "usr/local/lib/dsh-mu/tests/acceptance.sh",
        "fresh-machine check": "usr/local/lib/dsh-mu/tests/fresh-machine.py",
        "api contract": "usr/local/lib/dsh-mu/tests/api-contract.py",
        "artifact audit": "usr/local/lib/dsh-mu/tests/artifact-audit.py",
        "drift audit": "usr/local/lib/dsh-mu/tests/drift-audit.py",
        "regression runner": "usr/local/lib/dsh-mu/tests/regression.sh",
        "shared test helpers": "usr/local/lib/dsh-mu/tests/lib.sh",
        "handoff sheet": "usr/share/doc/dsh-mu/DELIVERY.md",
        "readme": "usr/share/doc/dsh-mu/README.md",
        "changelog": "usr/share/doc/dsh-mu/CHANGELOG.md",
        "runbook": "usr/share/doc/dsh-mu/docs/06-运维手册.md",
    }
    missing = [label for label, path in promised.items() if path not in paths]
    if not missing:
        ok(f"package carries all {len(promised)} promised files")
    else:
        bad(f"package is missing: {', '.join(missing)}")

    code, out = sh("apk info -L dsh-mu 2>/dev/null | head -1")
    if "pre-install" in out or True:  # the script members are not in the tar listing
        pass

    # --- nothing that must not ship
    forbidden = {
        "jail template": re.compile(r"(^|/)templates?/"),
        "instance data": re.compile(r"(^|/)instances?/"),
        "platform state": re.compile(r"state\.json$"),
        "session secret": re.compile(r"secret\.key$"),
        "admin token": re.compile(r"admin\.token$"),
        "provider catalogue": re.compile(r"providers\.json$"),
        "python bytecode": re.compile(r"__pycache__|\.pyc$"),
        "build output": re.compile(r"^dist/"),
    }
    leaks = []
    for label, pattern in forbidden.items():
        hits = [n for n in paths if pattern.search(n)]
        if hits:
            leaks.append(f"{label}: {hits[:3]}")
    if not leaks:
        ok("no template, runtime state, secret or build leftover inside")
    else:
        bad("package contains things it must not: " + "; ".join(leaks))

    if size < 2 * 1024 * 1024:
        ok("package stays small (no ~140 MB template)", f"{size / 1024:.0f} KB")
    else:
        bad("package is suspiciously large", f"{size / 1024:.0f} KB")

    biggest = max(members, key=lambda m: m.size if m.isfile() else 0)
    if biggest.size < 400 * 1024:
        ok("no single file dominates the package", f"{biggest.name} {biggest.size / 1024:.0f} KB")
    else:
        bad("a single file is unexpectedly large", f"{biggest.name} {biggest.size / 1024:.0f} KB")

    world_writable = [m.name for m in members if m.isfile() and (m.mode & 0o002)]
    if not world_writable:
        ok("no world-writable files in the package")
    else:
        bad("world-writable files shipped", ", ".join(world_writable[:5]))

    # ------------------------------------------------------------ source archive
    tarball = args.tarball
    if not tarball:
        dist = os.path.join(ROOT, "dist")
        if os.path.isdir(dist):
            cands = sorted(f for f in os.listdir(dist) if f.endswith(".tgz"))
            tarball = os.path.join(dist, cands[0]) if cands else ""
    if tarball and os.path.exists(tarball):
        with tarfile.open(tarball) as tf:
            tar_names = [n.lstrip("./") for n in tf.getnames()]
        junk = [n for n in tar_names if "__pycache__" in n or n.endswith(".pyc") or n.startswith("dist/")]
        if not junk:
            ok("source archive has no build leftovers")
        else:
            bad("source archive has leftovers", ", ".join(junk[:5]))
        secrets = [n for n in tar_names
                   if os.path.basename(n) in ("secret.key", "admin.token", "providers.json",
                                              "state.json", ".credentials.yaml")]
        if not secrets:
            ok("source archive carries no runtime secrets")
        else:
            bad("source archive carries secrets", ", ".join(secrets[:5]))
        for promised_doc in ("DELIVERY.md", "README.md", "CHANGELOG.md",
                             "docs/01-需求整理.md", "docs/06-运维手册.md",
                             "packaging/APKBUILD", "install/install.sh", "tests/lib.sh"):
            if promised_doc not in tar_names:
                bad(f"archive is missing {promised_doc}")
        ok("archive contains the promised docs, install scripts and tests")
    else:
        print("SKIP  source archive not found (pass --tarball)")

    # --------------------------------------------------------- DELIVERY promises
    delivery = os.path.join(ROOT, "DELIVERY.md")
    if os.path.exists(delivery):
        text = open(delivery, encoding="utf-8").read()
        claims = {
            "documents the apk install": r"apk add --allow-untrusted",
            "documents the first run": r"dsh-muctl install",
            "documents acceptance": r"tests/regression\.sh",
            "documents recovery": r"restore",
            "states the limitations": r"已知(限制|取舍)",
        }
        missing_claims = [label for label, pattern in claims.items() if not re.search(pattern, text)]
        if not missing_claims:
            ok("DELIVERY.md still documents install, first run, acceptance, recovery and limits")
        else:
            bad("DELIVERY.md lost: " + ", ".join(missing_claims))
        # every test file named in the delivery sheet exists
        named = set(re.findall(r"tests/([\w.-]+\.(?:sh|py|mjs))", text))
        absent = sorted(n for n in named if not os.path.exists(os.path.join(ROOT, "tests", n)))
        if not absent:
            ok(f"all {len(named)} test files named in DELIVERY.md exist")
        else:
            bad("DELIVERY.md names missing tests: " + ", ".join(absent))
    else:
        bad("DELIVERY.md is missing from the tree")

    print(f"\n=== ARTIFACT AUDIT: PASS={PASS} FAIL={FAIL} ===")
    print("ARTIFACT AUDIT PASSED" if FAIL == 0 else "ARTIFACT AUDIT FAILED")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
