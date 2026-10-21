#!/usr/bin/env python3
"""api-contract.py - the admin API must fail cleanly too.

    python3 tests/api-contract.py [--instance NAME]

The admin page shows `body.error` to the operator, so a bad request has to come
back as JSON with a sensible status - never as an HTML error page, a 500, or a
Python traceback in the backend log. This is the API-side twin of
tests/cli-contract.sh: deliberately wrong requests, every one of which must be
rejected politely, plus the read-only endpoints that must answer 200.

Nothing here changes platform state: every case is rejected before any write.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8787/api"
LOG = "/var/log/dsh-mu/admin.err.log"
PASS = 0
FAIL = 0
TOKEN = ""


def ok(label: str, extra: str = "") -> None:
    global PASS
    PASS += 1
    print(f"PASS  {label}" + (f"  -- {extra}" if extra else ""))


def bad(label: str, extra: str = "") -> None:
    global FAIL
    FAIL += 1
    print(f"FAIL  {label}" + (f"  -- {extra}" if extra else ""))


def call(method: str, path: str, body=None, token: str | None = None,
         raw_body: bytes | None = None) -> tuple[int, str, str]:
    """Returns (status, content-type, text)."""
    data = raw_body if raw_body is not None else (
        json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-DSH-MU-Admin", token)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - connection refused / timeout
        return 0, "", f"{type(exc).__name__}: {exc}"


def expect_error(label: str, method: str, path: str, *,
                 body=None, raw_body: bytes | None = None, token: str | None = None,
                 contains: str = "",
                 statuses=(400, 401, 403, 404, 405, 409, 422)) -> None:
    status, ctype, text = call(method, path, body, token if token is not None else TOKEN,
                               raw_body)
    if status not in statuses:
        bad(label, f"HTTP {status} (expected one of {statuses}): {text[:100]}")
        return
    if "json" not in ctype.lower():
        bad(label, f"HTTP {status} but Content-Type is {ctype!r}")
        return
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        bad(label, f"HTTP {status} but the body is not JSON: {text[:100]}")
        return
    if not isinstance(payload, dict) or "error" not in payload:
        bad(label, f"HTTP {status} but no 'error' field: {text[:100]}")
        return
    if contains and contains not in str(payload.get("error")):
        bad(label, f"HTTP {status} but the error is {payload.get('error')!r} "
                   f"(expected it to mention {contains!r})")
        return
    ok(label, f"HTTP {status}, error={str(payload.get('error'))[:40]!r}")


def expect_ok(label: str, method: str, path: str, *, body=None) -> None:
    status, ctype, text = call(method, path, body, TOKEN)
    if status != 200:
        bad(label, f"HTTP {status}: {text[:120]}")
        return
    try:
        json.loads(text)
    except json.JSONDecodeError:
        bad(label, f"HTTP 200 but the body is not JSON: {text[:80]}")
        return
    ok(label, "HTTP 200 JSON")


def main() -> int:
    global TOKEN
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", default="")
    args = ap.parse_args()

    try:
        TOKEN = open("/etc/dsh-mu/admin.token").read().strip()
    except OSError:
        print("cannot read /etc/dsh-mu/admin.token (run as root)")
        return 2

    instance = args.instance
    if not instance:
        try:
            st = json.load(open("/etc/dsh-mu/state.json"))
            instance = next(iter(st.get("instances") or {}), "noinst")
        except OSError:
            instance = "noinst"

    log_before = 0
    try:
        with open(LOG, errors="replace") as fh:
            log_before = len(fh.readlines())
    except OSError:
        LOG_UNREADABLE = True

    print(f"== API contract: {BASE}, instance={instance}, token={bool(TOKEN)} ==")

    # ---------------------------------------------------------- authorisation
    expect_error("no token at all", "GET", "/health", token="")
    expect_error("wrong token", "GET", "/health", token="deadbeef")

    # ------------------------------------------------------- unknown routes
    expect_error("GET an unknown route", "GET", "/nope")
    expect_error("POST an unknown route", "POST", "/nope", body={})
    expect_error("GET an unknown instance", "GET", "/instances/nosuchinstance")
    expect_error("acting on an unknown instance", "POST", "/instances/nosuchinstance/start")
    expect_error("a wrong method on a known route", "DELETE", "/health")

    # ------------------------------------------------------------- bad input
    # The page shows this text to the operator, so it must say what is wrong
    # instead of blaming a missing field.
    expect_error("malformed JSON body", "POST", "/users", raw_body=b"{not json",
                 contains="invalid JSON")
    expect_error("JSON body that is not an object", "POST", "/users", raw_body=b"[1,2,3]",
                 contains="must be an object")
    expect_error("user create without a name", "POST", "/users", body={"password": "x" * 12})
    expect_error("password change for an unknown user", "POST",
                 "/users/nosuchuser/password", body={"password": "x" * 12})
    expect_error("unknown config key", "POST", "/config", body={"key": "nope", "value": "1"})
    expect_error("config value of the wrong kind", "POST", "/config",
                 body={"key": "session_days", "value": "notanumber"})
    expect_error("negative port", "PATCH", f"/instances/{instance}", body={"port": -1})
    expect_error("port outside the allowed range", "PATCH", f"/instances/{instance}",
                 body={"port": 99999})
    expect_error("unknown instance provider", "POST", f"/instances/{instance}/provider",
                 body={"preset": "no-such-provider"})
    expect_error("api key for an unknown instance", "POST", "/instances/nosuch/key",
                 body={"key": "x" * 20})
    expect_error("unknown provider probe", "POST", "/provider-test", body={"name": "nope"})
    expect_error("deleting a built-in provider", "DELETE", "/providers/deepseek")
    expect_error("unknown user delete", "DELETE", "/users/nosuchuser")
    expect_error("wrong current password", "POST", "/me/password",
                 body={"current": "definitely-wrong", "next": "x" * 12})
    expect_error("an unknown sub-resource", "POST", f"/instances/{instance}/nonsense")
    # Limits and the port travel in PATCH /api/instances/<n> (that is what the
    # page submits), so a bad value there must be refused as well.
    expect_error("limits with a bad value", "PATCH", f"/instances/{instance}",
                 body={"cpu_cores": "notanumber"})
    expect_error("memory with a bad value", "PATCH", f"/instances/{instance}",
                 body={"mem_mb": "lots"})
    expect_error("pids with a bad value", "PATCH", f"/instances/{instance}",
                 body={"pids_num": "many"})

    # ------------------------------------------------------- read-only success
    for path in ("/health", "/overview", "/users", "/instances", "/doctor",
                 "/providers", "/config", "/audit", f"/instances/{instance}"):
        expect_ok(f"GET {path}", "GET", path)

    # ------------------------------------------------------ log must stay clean
    try:
        with open(LOG, errors="replace") as fh:
            lines = fh.readlines()
        new = lines[log_before:]
        tracebacks = [l for l in new if "Traceback (most recent call last)" in l]
        if tracebacks:
            bad("the backend logged a traceback during these requests",
                f"{len(tracebacks)} occurrence(s) in {LOG}")
        else:
            ok("the backend logged no traceback for any of these requests")
    except OSError:
        print(f"SKIP  {LOG} not readable; cannot check for tracebacks")

    print(f"\n=== API CONTRACT: PASS={PASS} FAIL={FAIL} ===")
    print("API CONTRACT PASSED" if FAIL == 0 else "API CONTRACT FAILED")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
