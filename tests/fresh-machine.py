#!/usr/bin/env python3
"""fresh-machine.py - end-to-end acceptance for a *new* dsh-mu host.

Implements the checklist in docs/05-打包与安装.md §6 as one command. It is meant
to be the last thing you run after `apk add dsh-mu` (or install.sh) on a clean
Alpine box, and the first thing you run to prove a machine is really usable.

    python3 fresh-machine.py                 # full run, cleans up after itself
    python3 fresh-machine.py --skip-install  # platform already installed
    python3 fresh-machine.py --keep          # keep the smoke user/instance
    python3 fresh-machine.py --json          # machine-readable report

Every step is printed as PASS/FAIL/WARN with the command that produced it. Exit
status is 0 only when nothing failed.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import random
import re
import string
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, "/usr/local/lib/dsh-mu/python")

try:
    import dshmuctl as mu
except ImportError:  # running from a source checkout
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cli"))
    import dshmuctl as mu  # type: ignore

RESULTS: list[dict] = []


def record(name: str, status: str, detail: str = "", fix: str = "") -> None:
    RESULTS.append({"step": name, "status": status, "detail": detail, "fix": fix})
    marker = {"PASS": "\033[32mPASS\033[0m", "FAIL": "\033[31mFAIL\033[0m",
              "WARN": "\033[33mWARN\033[0m", "INFO": "INFO"}[status]
    print(f"{marker}  {name}" + (f"  -- {detail}" if detail else ""))
    if fix and status in ("FAIL", "WARN"):
        print(f"      fix: {fix}")


def run(cmd: list[str], timeout: float = 900.0) -> tuple[int, str]:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return res.returncode, ((res.stdout or "") + (res.stderr or "")).strip()


def http_call(url: str, opener=None, method: str = "GET", payload: dict | None = None,
              follow: bool = True) -> tuple[int, str, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    if not follow:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
    opener = opener or urllib.request.build_opener()
    try:
        with opener.open(req, timeout=30) as res:
            body = res.read().decode(errors="replace")
            status, headers = res.status, dict(res.headers)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        status, headers = exc.code, dict(exc.headers)
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc), {}
    return status, body, headers


def login(base: str, user: str, password: str) -> tuple[object | None, str]:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    status, body, _ = http_call(f"{base}/login", opener)
    if status != 200:
        return None, f"GET /login returned {status}"
    match = re.search(r'name="csrf" value="([^"]+)"', body)
    if not match:
        return None, "no csrf token in the login form"
    form = urllib.parse.urlencode({"username": user, "password": password,
                                  "csrf": match.group(1), "next": "/"}).encode()
    req = urllib.request.Request(f"{base}/login", data=form)
    try:
        opener.open(req, timeout=30)
    except urllib.error.HTTPError:
        pass  # a 503 from the instance is handled by the caller
    return opener, ""


def random_password(n: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(random.SystemRandom().choice(alphabet) for _ in range(n))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="http://127.0.0.1")
    ap.add_argument("--user", default=None, help="test account (default: a generated 'smoke')")
    ap.add_argument("--password", default=None)
    ap.add_argument("--instance", default=None)
    ap.add_argument("--skip-install", action="store_true",
                    help="assume dsh-mu is already installed and configured")
    ap.add_argument("--keep", action="store_true", help="keep the smoke account and instance")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    base = args.host.rstrip("/")
    started = time.time()
    print(f"=== dsh-mu fresh-machine acceptance on {base} ===")

    # ---------------------------------------------------------------- 1. doctor
    state_exists = os.path.exists(mu.STATE_PATH)
    if not state_exists and args.skip_install:
        record("platform installed", "FAIL", f"{mu.STATE_PATH} missing",
               "drop --skip-install, or run: dsh-muctl install")
        return finish(started, args)
    if not state_exists:
        code, out = run(["dsh-muctl", "doctor", "--json"], timeout=180)
        try:
            summary = json.loads(out).get("summary", {})
        except json.JSONDecodeError:
            summary = {}
        if summary.get("FAIL"):
            record("doctor before install", "WARN",
                   f"{summary['FAIL']} FAIL before installing (expected on a bare machine)")
        code, out = run(["dsh-muctl", "install"], timeout=3600)
        if code != 0:
            record("dsh-muctl install", "FAIL", out[-400:], "fix the doctor FAILs, then re-run install")
            return finish(started, args)
        record("dsh-muctl install", "PASS", "packages, DSH runtime, jail template, gateway, services")
    else:
        record("platform installed", "PASS", mu.STATE_PATH)

    checks = mu.doctor_checks(mu.load_state())
    fails = [c for c in checks if c["status"] == mu.DOCTOR_FAIL]
    warns = [c for c in checks if c["status"] == mu.DOCTOR_WARN]
    record("doctor", "PASS" if not fails else "FAIL",
           f"{len(checks) - len(fails) - len(warns)} PASS, {len(warns)} WARN, {len(fails)} FAIL"
           + ("" if not fails else ": " + ", ".join(c["check"] for c in fails)),
           "" if not fails else "dsh-muctl doctor --fix")

    # --------------------------------------------------------- 2. admin account
    st = mu.load_state()
    admins = [n for n, u in st["users"].items() if u.get("role") == "admin" and u.get("enabled", True)]
    if admins:
        record("admin account exists", "PASS", ", ".join(sorted(admins)))
    else:
        code, out = run(["dsh-muctl", "bootstrap"], timeout=60)
        record("admin bootstrap", "PASS" if code == 0 else "FAIL", out[-200:])

    # ------------------------------------------------------------- 3. smoke user
    smoke_user = args.user
    smoke_pass = args.password
    smoke_inst = args.instance
    created = False
    if not smoke_user:
        smoke_user = "smoke"
        smoke_pass = smoke_pass or random_password()
        if smoke_user not in mu.load_state()["users"]:
            code, out = run(["dsh-muctl", "user", "add", smoke_user, "--password", smoke_pass,
                            "--instance", "auto"], timeout=120)
            if code != 0:
                record("create smoke user", "FAIL", out[-300:], "check `dsh-muctl user add --help`")
                return finish(started, args)
            created = True
            record("create smoke user", "PASS", f"{smoke_user} (instance {smoke_user})")
        else:
            record("smoke user", "WARN", f"{smoke_user} already exists; --password ignored "
                                        "(pass --user/--password to test a real account)")
        smoke_inst = smoke_inst or smoke_user
    else:
        record("smoke user", "INFO", f"using the supplied account {smoke_user}")
        smoke_inst = smoke_inst or smoke_user

    # ------------------------------------------------------------- 4. instance up
    # An admin-only account legitimately has no instance: the instance-dependent
    # steps are then reported as skipped instead of failed.
    account_has_instance = smoke_inst in mu.load_state()["instances"]
    if not account_has_instance:
        record("instance serving", "INFO",
               f"{smoke_user} has no instance (admin-only account); "
               "instance and end-to-end steps are skipped")
    if account_has_instance:
        code, out = run(["dsh-muctl", "instance", "start", smoke_inst], timeout=300)
        deadline = time.time() + 180
        port = int(mu.load_state()["instances"][smoke_inst].get("port", 0))
        while time.time() < deadline and not mu.port_in_use(port):
            time.sleep(3)
        alive = mu.port_in_use(port)
        record("instance serving", "PASS" if alive else "FAIL",
               f"{smoke_inst} on port {port}" + ("" if alive else " (still closed after 180s)"),
               "" if alive else f"dsh-muctl instance logs {smoke_inst}")
        if alive:
            run(["dsh-muctl", "instance", "refresh-cookie", smoke_inst], timeout=300)

    # --------------------------------------------------------- 5. gateway / proxy
    status, body, headers = http_call(f"{base}/", follow=False)
    ok = status == 302 and "/login" in headers.get("Location", "")
    record("anonymous / redirects to login", "PASS" if ok else "FAIL",
           f"status {status} -> {headers.get('Location', '')}")

    opener, err = login(base, smoke_user, smoke_pass) if smoke_pass else (None, "no password")
    if opener and not account_has_instance:
        # No instance for this account: the gateway must answer with its own
        # explanatory 503 page rather than a bare proxy error.
        status, body, _ = http_call(f"{base}/", opener)
        expected = status == 503 and "实例" in body
        record("account without an instance gets the gateway 503 page",
               "PASS" if expected else "FAIL",
               f"status {status}, {len(body)} bytes",
               "" if expected else "expected our 503 page")
    elif opener:
        status, body, _ = http_call(f"{base}/", opener)
        served = status == 200 and "DeepSeek Harness" in body
        record("authenticated / serves the harness", "PASS" if served else "FAIL",
               f"status {status}, {len(body)} bytes",
               "" if served else f"dsh-muctl instance logs {smoke_inst}")
        try:
            ops_token = open("/etc/dsh-mu/admin.token").read().strip()
        except OSError:
            ops_token = ""
        status, body, _ = http_call(f"{base}/mu-api/status?t={ops_token}", opener)
        record("loopback ops API", "PASS" if status == 200 else "WARN", f"status {status}")
    else:
        record("authenticated / serves the harness", "WARN", f"skipped: {err}")

    # ------------------------------------------------------------- 6. admin page
    admin_user = admins[0] if admins else None
    admin_pass = args.password if args.user == admin_user else None
    if admin_user and admin_pass:
        aop, err = login(base, admin_user, admin_pass)
        if aop:
            status, body, _ = http_call(f"{base}/admin/", aop)
            record("admin page", "PASS" if status == 200 and "管理台" in body else "FAIL",
                   f"status {status}", "" if status == 200 else "dsh-muctl admin status")
            status, body, _ = http_call(f"{base}/admin/api/overview", aop)
            ok = status == 200
            record("admin API overview", "PASS" if ok else "FAIL", f"status {status}")
            if ok:
                data = json.loads(body)
                record("admin sees the instance", "PASS" if data["counts"]["instances"] >= 1 else "WARN",
                       f"counts={data['counts']}")
        else:
            record("admin page", "WARN", f"login failed: {err}")
    else:
        status, body, headers = http_call(f"{base}/admin/", follow=False)
        record("admin page is gated", "PASS" if status in (302, 401) else "FAIL",
               f"anonymous /admin/ -> {status}")
        if admin_user:
            record("admin page", "INFO",
                   f"pass --user {admin_user} --password <pw> to exercise the admin UI")

    # --------------------------------------------------- 7. supplied account e2e
    if args.user and args.password and account_has_instance:
        # The host may have no node of its own (the DSH runtime lives in the gateway
        # template); DSH_MU_NODE lets the caller supply a prefix such as
        # "chroot <rootfs> /usr/bin/node", and DSH_MU_TEST_DIR the staged suite path.
        node_cmd = os.environ.get("DSH_MU_NODE", "").split() or ["node"]
        suite = os.environ.get("DSH_MU_TEST_DIR") or f"{mu.LIB}/tests"
        code, out = run([*node_cmd, f"{suite}/selftest.mjs", "--user", args.user,
                         "--pass", args.password], timeout=600)
        record("selftest", "PASS" if code == 0 else "FAIL",
               "ALL CHECKS PASSED" if code == 0 else out[-300:])
        acc = os.environ.get("DSH_MU_TEST_DIR_ACC") or f"{mu.LIB}/tests/acceptance.sh"
        code, out = run(["sh", acc, smoke_inst, args.user, args.password], timeout=900)
        record("acceptance suite", "PASS" if code == 0 else "FAIL",
               out.splitlines()[-1] if out else "", "" if code == 0 else out[-300:])

    # ------------------------------------------------------------------ cleanup
    if created and not args.keep:
        run(["dsh-muctl", "instance", "stop", smoke_inst], timeout=300)
        run(["dsh-muctl", "instance", "del", smoke_inst, "--purge"], timeout=600)
        run(["dsh-muctl", "user", "del", smoke_user], timeout=120)
        record("cleanup", "PASS", f"removed smoke account {smoke_user} and its instance")
    elif created:
        record("cleanup", "INFO", f"kept {smoke_user}/{smoke_pass} (password shown once)")
    return finish(started, args)


def finish(started: float, args) -> int:
    counts: dict[str, int] = {}
    for r in RESULTS:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print()
    print(f"=== FRESH-MACHINE RESULT: PASS={counts.get('PASS', 0)} FAIL={counts.get('FAIL', 0)} "
          f"WARN={counts.get('WARN', 0)} ({int(time.time() - started)}s) ===")
    if args.json:
        print(json.dumps({"results": RESULTS, "summary": counts}, indent=2, ensure_ascii=False))
    failed = counts.get("FAIL", 0)
    print("FRESH-MACHINE ACCEPTANCE PASSED" if not failed else "FRESH-MACHINE ACCEPTANCE FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
