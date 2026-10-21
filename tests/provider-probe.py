#!/usr/bin/env python3
"""provider-probe.py - the page's "test connection" button.

    python3 tests/provider-probe.py

`POST /api/provider-test` sends one tiny completion and reports what came back.
It is the diagnostic an administrator trusts when a model stops answering, so:

  * a 200 with a body that contains no model reply must NOT be reported as a
    working provider (that would colour a broken endpoint green)
  * a 5xx, a refused connection and a nonsense URL must come back as a *result*
    (`ok: false` plus a readable detail), never as a crash or a hung request
  * the credential must never appear anywhere in the reply
  * the probe runs in its own thread: a slow endpoint must not freeze the
    rest of the admin API

It stands up a local stub endpoint, registers it as a throwaway custom provider,
probes it, and removes everything again.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BACKEND = "http://127.0.0.1:8787"
CLI = os.environ.get("DSHMU_CLI", "/usr/local/sbin/dsh-muctl")
TOKEN_PATH = "/etc/dsh-mu/admin.token"
FAKE_KEY = "sk-probe-DO-NOT-LEAK-1234567890"
PASS = 0
FAIL = 0

ROUTES: dict[str, tuple[int, str, float]] = {
    "/reply/chat/completions": (200, json.dumps({"choices": [{"message": {"content": "pong"}}]}), 0.0),
    "/garbage/chat/completions": (200, "definitely not json", 0.0),
    "/boom/chat/completions": (500, "upstream exploded", 0.0),
    "/slow/chat/completions": (200, json.dumps({"choices": [{"message": {"content": "slow"}}]}), 4.0),
}


class Stub(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        status, body, delay = ROUTES.get(self.path, (404, "no such route", 0.0))
        if delay:
            time.sleep(delay)
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):  # keep the test output clean
        return


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


def call(path: str, payload: dict | None = None, method: str = "POST",
         timeout: float = 60) -> tuple[int, dict]:
    with open(TOKEN_PATH, encoding="utf-8") as fh:
        token = fh.read().strip()
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{BACKEND}{path}", data=data, method=method)
    req.add_header("X-DSH-MU-Admin", token)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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

    stub = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    port = stub.server_address[1]
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    name = f"probe{os.getpid() % 100000}"
    print(f"== provider probe: stub={base} provider={name} ==")

    status, body = call("/api/providers", {"name": name, "kind": "pi-ai",
                                           "base_url": f"{base}/reply", "route": name,
                                           "model": "stub-model",
                                           "display_name": "probe stub"})
    if status == 200:
        ok("registered a throwaway custom provider")
    else:
        bad("could not register the stub provider", f"{status} {str(body)[:90]}")
        stub.shutdown()
        return 1

    try:
        status, body = call("/api/provider-test", {"name": name, "api_key": FAKE_KEY,
                                                   "model": "stub-model"})
        # point the provider at the good route by name
        if status == 200 and body.get("ok") is True and body.get("status") == 200:
            ok("a real model reply is reported as working", f"HTTP {body.get('status')}")
        else:
            bad("a good reply was not reported as ok", f"{status} {json.dumps(body)[:110]}")

        if FAKE_KEY not in json.dumps(body):
            ok("the credential is not echoed anywhere in the reply")
        else:
            bad("the reply leaked the API key")

        if body.get("key_source") == "argument":
            ok("the reply says where the credential came from", "argument")
        else:
            bad("key_source is wrong", str(body.get("key_source")))

        call("/api/providers", {"name": name, "kind": "pi-ai", "base_url": f"{base}/garbage",
                                "route": name, "model": "stub-model"})
        status, body = call("/api/provider-test",
                            {"name": name, "model": "stub-model", "api_key": "x" * 12})
        if status == 200 and body.get("ok") is False:
            ok("a 200 with an unparseable body is NOT reported as working",
               f"detail={(body.get('detail') or '')[:48]}")
        else:
            bad("a 200 with a junk body was reported as working",
                f"ok={body.get('ok')} detail={(body.get('detail') or '')[:40]}")

        # the routes are chosen by the provider definition, so probe them by
        # re-registering the provider with a different base_url
        call("/api/providers", {"name": name, "kind": "pi-ai", "base_url": f"{base}/boom",
                                "route": name, "model": "stub-model"})
        status, body = call("/api/provider-test", {"name": name, "model": "stub-model"})
        if status == 200 and body.get("ok") is False and body.get("status") == 500:
            ok("an upstream error is reported with its status and body",
               f"HTTP {body.get('status')} {(body.get('detail') or '')[:30]}")
        else:
            bad("an upstream error was mishandled", f"{status} {json.dumps(body)[:100]}")

        call("/api/providers", {"name": name, "kind": "pi-ai", "base_url": "http://127.0.0.1:1",
                                "route": name, "model": "stub-model"})
        status, body = call("/api/provider-test", {"name": name, "model": "stub-model"})
        if status == 200 and body.get("ok") is False and body.get("status") == 0:
            ok("a refused connection is a readable result, not a crash",
               f"detail={(body.get('detail') or '')[:44]}")
        else:
            bad("a refused connection was mishandled", f"{status} {json.dumps(body)[:100]}")

        call("/api/providers", {"name": name, "kind": "pi-ai", "base_url": "file:///etc",
                                "route": name, "model": "stub-model"})
        status, body = call("/api/provider-test", {"name": name, "model": "stub-model"})
        if status == 200 and body.get("ok") is False and "root:" not in json.dumps(body):
            ok("a file:// base URL fails cleanly and leaks nothing",
               f"detail={(body.get('detail') or '')[:40]}")
        else:
            bad("a file:// base URL behaved oddly", f"{status} {json.dumps(body)[:100]}")

        status, body = call("/api/provider-test", {"name": "no-such-provider"})
        if status == 404:
            ok("an unknown provider is a 404")
        else:
            bad("an unknown provider was not 404", f"{status}")

        # a slow endpoint must not freeze the rest of the API
        call("/api/providers", {"name": name, "kind": "pi-ai", "base_url": f"{base}/slow",
                                "route": name, "model": "stub-model"})
        result: dict = {}

        def slow_probe():
            result["value"] = call("/api/provider-test", {"name": name, "model": "stub-model"},
                                   timeout=60)

        worker = threading.Thread(target=slow_probe, daemon=True)
        worker.start()
        time.sleep(1.0)
        started = time.monotonic()
        status, overview = call("/api/overview", method="GET", timeout=10)
        elapsed = time.monotonic() - started
        if status == 200 and elapsed < 3.0:
            ok("a slow probe does not block other admin requests",
               f"overview answered in {elapsed:.2f}s while the probe ran")
        else:
            bad("the admin API was blocked by a running probe",
                f"status={status} after {elapsed:.1f}s")
        worker.join(timeout=60)
        if result.get("value", (0, {}))[1].get("ok") is True:
            ok("the slow but valid probe still succeeds afterwards")
        else:
            bad("the slow probe did not finish cleanly", str(result)[:90])
    finally:
        stub.shutdown()
        if not args.keep:
            call(f"/api/providers/{name}", method="DELETE")

    status, body = call("/api/providers", method="GET")
    if name not in (body.get("providers") or {}):
        ok("the throwaway provider is gone")
    else:
        bad("the throwaway provider is still registered")

    line = ""
    code, out = sh(f"{CLI} doctor")
    for candidate in out.splitlines():
        if candidate.startswith("doctor:"):
            line = candidate
    if "0 FAIL" in line and "0 WARN" in line:
        ok("doctor is clean after the drill", line)
    else:
        bad("doctor is not clean after the drill", line or "no doctor line")

    print(f"\n=== PROVIDER PROBE: PASS={PASS} FAIL={FAIL} ===")
    print("PROVIDER PROBE PASSED" if FAIL == 0 else "PROVIDER PROBE FAILED")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
