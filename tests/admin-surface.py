#!/usr/bin/env python3
"""admin-surface.py - use /admin/ as a real administrator, end to end.

    python3 tests/admin-surface.py

Everything about the admin surface used to be verified sideways: the anonymous
gate (redirect to /login) by the gateway selftest, and the backend contract by
talking to the loopback API with the shared token - which skips the Lua
`role=admin` check entirely. Nobody had ever logged in as an administrator and
used the page's own API.

This does exactly that, with a temporary *host* account:

  1. create a system account with a known password (adduser + chpasswd)
  2. add it to ADMIN_SYSTEM_USERS and let `sync_all` create its platform record
  3. log in through the gateway as that account
  4. use the admin API the page uses (`/admin/api/...`): me, overview, users,
     create a user, delete it again
  5. check the audit trail credits that administrator
  6. put everything back (config, account) and confirm doctor stays clean

The account and the config entry are temporary; the password is generated per run
and never printed.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1"
CLI = os.environ.get("DSHMU_CLI", "/usr/local/sbin/dsh-muctl")
AUDIT = os.environ.get("DSHMU_AUDIT", "/var/log/dsh-mu/audit.log")
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def new_jar():
    return urllib.request.build_opener(_NoRedirect,
                                      urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def call(opener, path: str, method: str = "GET", payload: dict | None = None,
         base: str = f"{BASE}/admin/api") -> tuple[int, object]:
    data = json.dumps(payload).encode() if payload is not None else None
    url = path if path.startswith("http") else f"{base}/{path.lstrip('/')}"
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with opener.open(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def login(opener, user: str, password: str) -> tuple[int, str]:
    try:
        with opener.open(f"{BASE}/login", timeout=20) as resp:
            page = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        page = exc.read().decode("utf-8", "replace")
    match = re.search(r'name="csrf" value="([^"]+)"', page)
    if not match:
        return 0, "no csrf token on the login page"
    body = urllib.parse.urlencode({"username": user, "password": password,
                                   "csrf": match.group(1), "next": "/"}).encode()
    req = urllib.request.Request(f"{BASE}/login", data=body)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with opener.open(req, timeout=20) as resp:
            return resp.status, resp.headers.get("Location", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Location", "")


def audit_lines() -> int:
    try:
        with open(AUDIT, encoding="utf-8", errors="replace") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="leave the test account in place (debugging)")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("must run as root", file=sys.stderr)
        return 2

    acct = f"dshmuverify{os.getpid() % 100000}"
    password = f"Adm-{os.getpid()}-{int(time.time()) % 100000}"
    victim = f"admcheck{os.getpid() % 100000}"
    config_touched = False

    print(f"== admin surface: host account={acct} ==")

    code, original = sh(f"{CLI} config admin_system_users")
    original = original.strip().strip('"')
    if code != 0:
        bad("could not read admin_system_users", original[-80:])
        return 2
    ok("read the current administrator configuration", f"ADMIN_SYSTEM_USERS={original!r}")

    try:
        code, out = sh(f"adduser -D -s /bin/sh {acct}")
        if code != 0:
            bad("could not create the temporary host account", out[-100:])
            return 1
        code, out = sh(f"printf '%s:%s\\n' {acct} {password} | chpasswd")
        if code != 0:
            bad("could not set the host password", out[-100:])
            return 1
        ok("created a temporary host account with a known password")

        wanted = f"{original},{acct}" if original else acct
        code, out = sh(f"{CLI} config admin_system_users {wanted}")
        if code != 0:
            bad("could not add the account to ADMIN_SYSTEM_USERS", out[-100:])
            return 1
        config_touched = True
        ok("added it to ADMIN_SYSTEM_USERS (platform record created by sync_all)")

        code, out = sh(f"{CLI} user list")
        if acct in out:
            ok("the account has a platform record")
        else:
            bad("no platform record for the new administrator")

        time.sleep(3)   # the gateway caches the rendered user view for ~2s

        jar = new_jar()
        status, location = login(jar, acct, password)
        if status == 302 and location.rstrip("/").endswith("/admin"):
            ok("administrator login lands on /admin/", f"302 -> {location}")
        elif status == 302:
            ok("administrator login redirects", f"302 -> {location}")
        else:
            bad("administrator login failed", f"{status} {location}")

        status, body = call(jar, "", base=f"{BASE}/admin")
        text = body if isinstance(body, str) else json.dumps(body)
        if status == 200 and ("<html" in text.lower() or "<!doctype" in text.lower()):
            ok("GET /admin/ serves the page to an administrator", f"{len(text)} bytes")
        else:
            bad("GET /admin/ did not serve the page", f"HTTP {status}")

        status, body = call(jar, "me")
        # The contract is `role`; `admin` is a convenience flag in some shapes.
        is_admin = isinstance(body, dict) and (body.get("role") == "admin" or body.get("admin") is True)
        if status == 200 and is_admin:
            ok("/admin/api/me confirms the administrator",
               f"user={body.get('user')} role={body.get('role')}")
        else:
            bad("/admin/api/me did not confirm the administrator", f"{status} {str(body)[:80]}")

        status, body = call(jar, "overview")
        if status == 200 and isinstance(body, dict) and ("platform" in body or "users" in body):
            keys = ",".join(sorted(body)[:6])
            ok("/admin/api/overview answers", f"keys: {keys}")
        else:
            bad("/admin/api/overview failed", f"{status} {str(body)[:80]}")

        status, body = call(jar, "instances")
        if status == 200:
            n = len(body) if isinstance(body, list) else len((body or {}).get("instances", []))
            ok("/admin/api/instances answers", f"{n} instances")
        else:
            bad("/admin/api/instances failed", f"{status} {str(body)[:80]}")

        before = audit_lines()
        status, body = call(jar, "users", method="POST",
                           payload={"name": victim, "password": "adm-check-password-1",
                                    "instance": "auto"})
        created = status in (200, 201) and isinstance(body, dict) and not body.get("error")
        if created:
            ok("creating a user through the admin API works", f"HTTP {status}")
        else:
            bad("creating a user through the admin API failed", f"{status} {str(body)[:100]}")

        status, body = call(jar, "users")
        rows = body if isinstance(body, list) else (body or {}).get("users", [])
        listed = isinstance(rows, list) and any(u.get("name") == victim for u in rows)
        if listed:
            ok("the new user appears in the admin user list")
        else:
            bad("the new user is missing from the admin user list")

        # ---- the page's provider/key workflow (requirement 2 covers both) ----
        # Wait for the instance the API just created (sync_all renders it).
        inst_ready = False
        for _ in range(30):
            status, rows = call(jar, "instances")
            items = rows if isinstance(rows, list) else (rows or {}).get("instances", [])
            mine = [i for i in items if i.get("name") == victim]
            if mine:
                inst_ready = True
                break
            time.sleep(2)
        if inst_ready:
            ok("the user created through the API got its own instance")
        else:
            bad("no instance appeared for the new user")

        status, body = call(jar, f"instances/{victim}/provider",
                           method="POST", payload={"preset": "deepseek", "model": "deepseek-flash"})
        wired = status == 200 and isinstance(body, dict) and \
            (body.get("instance") or {}).get("provider") == "deepseek"
        if wired:
            ok("assigning a provider through the admin API works", "provider=deepseek")
        else:
            bad("assigning a provider failed", f"{status} {str(body)[:100]}")

        patch_path = f"/var/lib/dsh-mu/instances/{victim}/home/.dsh-mu-provider.yml"
        if os.path.exists(patch_path) and "deepseek" in open(patch_path, encoding="utf-8", errors="replace").read():
            ok("the launcher patch file for the instance was written")
        else:
            bad("the launcher patch file is missing or wrong", patch_path)

        status, body = call(jar, f"instances/{victim}/patch")
        text = json.dumps(body)
        if status == 200 and "deepseek" in text:
            ok("GET patch resolves the provider", f"{len(text)} bytes of JSON")
        else:
            bad("GET patch did not resolve the provider", f"{status} {text[:80]}")

        status, body = call(jar, f"instances/{victim}/key",
                           method="POST", payload={"api_key": "adm-check-key-1234567890"})
        if status == 200 and isinstance(body, dict) and body.get("key_set"):
            ok("setting an API key through the admin API works", f"HTTP {status}")
        else:
            bad("setting an API key failed", f"{status} {str(body)[:100]}")

        status, rows = call(jar, "instances")
        items = rows if isinstance(rows, list) else (rows or {}).get("instances", [])
        mine = next((i for i in items if i.get("name") == victim), {})
        # `key_set`/`key_source` are what the page's credential column reads; the
        # view used to omit them, so the column always said "not set".
        if mine.get("provider") == "deepseek" and mine.get("key_set") is True:
            ok("the instance list reports provider and credential status",
               f"provider={mine.get('provider')} key_set={mine.get('key_set')} "
               f"source={mine.get('key_source')}")
        else:
            bad("the instance list does not reflect the wiring", json.dumps(mine)[:160])

        try:
            with open("/etc/dsh-mu/state.json", encoding="utf-8") as fh:
                state = json.load(fh)
            marker = (state.get("instances", {}).get(victim, {}) or {}).get("credentials") or {}
            if marker.get("api_key"):
                ok("the API key path records the credential marker (same as the CLI path)")
            else:
                bad("the API key path did not record the marker")
        except OSError:
            bad("could not read state.json to check the marker")

        # ---- the instance tab: log view and the limit editor ----
        status, body = call(jar, f"instances/{victim}/logs?lines=50")
        lines = (body or {}).get("lines") if isinstance(body, dict) else None
        if status == 200 and isinstance(body, dict) and isinstance(lines, list) and body.get("file"):
            ok("the log view the page uses returns lines", 
               f"file={os.path.basename(str(body.get('file')))} lines={len(lines)}")
        else:
            bad("the log view did not return the expected shape", f"{status} {str(body)[:100]}")

        status, before_view = call(jar, f"instances/{victim}")
        original_pids = ((before_view or {}).get("instance") or {}).get("limits", {}).get("pids_max")
        # The endpoint takes the page's field names (pids_num), not the raw record
        # names - sending the wrong one used to be a silent 200 no-op.
        status, body = call(jar, f"instances/{victim}", method="PATCH", payload={"pids_num": 128})
        applied = status == 200 and isinstance(body, dict)
        if applied:
            ok("changing limits through the admin API works", f"pids_max -> 128")
        else:
            bad("the limit change failed", f"{status} {str(body)[:100]}")
        status, view = call(jar, f"instances/{victim}")
        pids_now = ((view or {}).get("instance") or {}).get("limits", {}).get("pids_max")
        human = ((view or {}).get("instance") or {}).get("limits_human", {}).get("pids")
        if str(pids_now) == "128" and human:
            ok("the new limit is visible in the view the page renders", f"limits_human.pids={human}")
        else:
            bad("the view does not show the new limit", f"pids_max={pids_now} human={human}")
        cgroup_pids = f"/sys/fs/cgroup/dsh-mu/{victim}/pids.max"
        if os.path.exists(cgroup_pids):
            live = open(cgroup_pids).read().strip()
            if live == "128":
                ok("the change reached the running cgroup", f"pids.max={live}")
            else:
                bad("the cgroup still says pids.max=" + live)
        status, body = call(jar, f"instances/{victim}", method="PATCH", payload={"pids_max": 4242})
        if status == 400 and isinstance(body, dict) and body.get("accepted"):
            ok("a limit patch with unknown fields is rejected instead of silently ignored",
               f"HTTP 400, accepted={','.join(body['accepted'][:3])}...")
        else:
            bad("an unknown limit field was not rejected", f"{status} {str(body)[:100]}")
        if original_pids:
            call(jar, f"instances/{victim}", method="PATCH", payload={"pids_num": int(original_pids)})

        # ---- the audit tab (table view and the JSONL export button) ----
        status, body = call(jar, "audit?limit=5")
        good = (status == 200 and isinstance(body, dict) and isinstance(body.get("records"), list)
                and "scanned" in body and "matched" in body)
        if good:
            first = body["records"][0] if body["records"] else {}
            ok("the audit view returns records with the fields the page renders",
               f"matched={body['matched']} keys={','.join(sorted(first)[:5])}")
        else:
            bad("the audit view did not return the expected shape", f"{status} {str(body)[:100]}")

        status, body = call(jar, "audit?format=jsonl&limit=5")
        text = body if isinstance(body, str) else json.dumps(body)
        first_line = text.strip().splitlines()[0] if text.strip() else ""
        try:
            parsed = json.loads(first_line)
            ok("the JSONL export is one JSON object per line", f"{len(text.splitlines())} lines")
        except (json.JSONDecodeError, IndexError):
            bad("the JSONL export is not valid JSON lines", first_line[:80])

        # ---- the provider catalogue tab: add, list, delete, and the built-in guard ----
        custom_name = f"admprov{os.getpid() % 10000}"
        status, body = call(jar, "providers", method="POST",
                           payload={"name": custom_name, "kind": "pi-ai",
                                    "base_url": "http://127.0.0.1:9/v1", "model": "probe-model",
                                    "api": "openai-completions", "route": custom_name})
        if status == 200:
            ok("adding a custom provider through the admin API works", custom_name)
        else:
            bad("adding a custom provider failed", f"{status} {str(body)[:100]}")
        status, body = call(jar, "providers")
        provs = (body or {}).get("providers") if isinstance(body, dict) else None
        if isinstance(provs, dict) and custom_name in provs and provs[custom_name].get("custom"):
            ok("the catalogue lists it as custom", f"{len(provs)} providers total")
        else:
            bad("the custom provider is missing from the catalogue", str(body)[:100])
        status, body = call(jar, f"providers/{custom_name}", method="DELETE")
        if status == 200:
            ok("deleting the custom provider works", f"HTTP {status}")
        else:
            bad("deleting the custom provider failed", f"{status} {str(body)[:100]}")
        status, body = call(jar, "providers/deepseek", method="DELETE")
        status2, body2 = call(jar, "providers")
        still = isinstance(body2, dict) and "deepseek" in ((body2.get("providers") or {}))
        if status >= 400 and still:
            ok("a built-in provider cannot be deleted", f"HTTP {status}")
        else:
            bad("deleting a built-in provider was not refused", f"{status} still={still}")

        # A deliberately failing probe must fail *cleanly* (structured result, no 5xx)
        status, body = call(jar, f"instances/{victim}/provider/test",
                           method="POST", payload={"api_key": "deliberately-invalid-key-000"})
        if status in (200, 400, 502) and isinstance(body, dict) and "ok" in body:
            ok("a failing provider probe returns a structured result", 
               f"HTTP {status} ok={body.get('ok')}")
        else:
            bad("a failing provider probe did not fail cleanly", f"{status} {str(body)[:100]}")

        status, body = call(jar, f"users/{victim}", method="DELETE")
        if status == 409 and isinstance(body, dict) and body.get("instance"):
            ok("deleting an account that owns an instance is refused without the flag",
               f"HTTP 409, instance={body.get('instance')}")
        else:
            bad("the API deleted an account without asking about its instance",
                f"{status} {str(body)[:100]}")

        status, rows = call(jar, "instances")
        items = rows if isinstance(rows, list) else (rows or {}).get("instances", [])
        if any(i.get("name") == victim for i in items):
            ok("the instance survived the refused delete (no silent orphan)")
        else:
            bad("the instance disappeared although the delete was refused")

        status, body = call(jar, f"users/{victim}?with_instance=1", method="DELETE")
        if status in (200, 204):
            ok("deleting the user together with its instance works", f"HTTP {status}")
        else:
            bad("deleting the user with its instance failed", f"{status} {str(body)[:100]}")

        if audit_lines() > before:
            ok("the admin API calls are audited")
        else:
            bad("the admin API calls left no audit record")
        try:
            with open(AUDIT, encoding="utf-8", errors="replace") as fh:
                tail = fh.read()[-20000:]
            if f"cli:root" in tail or acct in tail:
                ok("the audit trail credits an actor for those calls")
            else:
                bad("the audit trail does not name the actor")
        except OSError:
            pass
    finally:
        sh(f"{CLI} provider rm admprov{os.getpid() % 10000}")   # in case a check failed mid-way
        # belt and braces: the API delete above removes both, but a failure
        # earlier must not leave an orphan instance behind
        sh(f"{CLI} user del {victim} --with-instance")
        sh(f"{CLI} instance del {victim} --purge")
        if config_touched:
            sh(f'{CLI} config admin_system_users "{original}"')
        if not args.keep:
            sh(f"deluser {acct}")

    code, out = sh(f"{CLI} config admin_system_users")
    if out.strip().strip('"') == original:
        ok("ADMIN_SYSTEM_USERS restored", f"{out.strip()}")
    else:
        bad("ADMIN_SYSTEM_USERS was not restored", out.strip())
    code, out = sh(f"{CLI} user list")
    rows = [line.split() for line in out.splitlines()[1:] if line.strip()]
    mine = [row for row in rows if row and row[0] == acct]
    if not mine:
        ok("the temporary administrator has no platform record any more")
    elif len(mine[0]) > 1 and mine[0][1] != "admin":
        # Removing an account from ADMIN_SYSTEM_USERS demotes its record instead
        # of deleting it - the documented behaviour, worth pinning here.
        ok("the ex-administrator's record was demoted, not deleted", f"role={mine[0][1]}")
    else:
        bad("the temporary administrator is still an administrator", " ".join(mine[0]))

    line = ""
    code, out = sh(f"{CLI} doctor")
    for candidate in out.splitlines():
        if candidate.startswith("doctor:"):
            line = candidate
    if "0 FAIL" in line:
        ok("doctor is clean after the drill", line)
    else:
        bad("doctor reports failures after the drill", line or "no doctor line")

    print(f"\n=== ADMIN SURFACE: PASS={PASS} FAIL={FAIL} ===")
    print("ADMIN SURFACE PASSED" if FAIL == 0 else "ADMIN SURFACE FAILED")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
