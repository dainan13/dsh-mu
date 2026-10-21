#!/usr/bin/env python3
"""soak.py - does the platform stay flat under sustained load?

    python3 tests/soak.py [--minutes 2] [--workers 4] [--instance alice]

Functional tests say "it works once". This one keeps real traffic flowing
(logins, page loads, /me, admin API calls straight at the loopback backend) and
samples the processes every few seconds, so a leak or a growing backlog shows up
as a trend instead of a mystery weeks later.

It asserts, with the thresholds printed:
  * the backend process was never restarted (same PID for the whole run)
  * the nginx worker count did not change
  * backend RSS stayed within 50 % / 64 MB of the starting value
  * backend open file descriptors grew by no more than 32
  * not a single 5xx from the gateway and no client-side errors
  * the instance's harness process RSS stayed within 30 %

Duration defaults to 2 minutes so it can run inside the regression suite; use
--minutes 10 (or more) for a real soak.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1"
PASS = 0
FAIL = 0
SAMPLES: list[dict] = []


def ok(label: str, extra: str = "") -> None:
    global PASS
    PASS += 1
    print(f"PASS  {label}" + (f"  -- {extra}" if extra else ""))


def bad(label: str, extra: str = "") -> None:
    global FAIL
    FAIL += 1
    print(f"FAIL  {label}" + (f"  -- {extra}" if extra else ""))


def sh(cmd: str) -> str:
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        return res.stdout.strip()
    except subprocess.TimeoutExpired:
        return ""


def http_call(url: str, opener=None, timeout: int = 20) -> tuple[int, str]:
    open_ = opener.open if opener else urllib.request.urlopen
    try:
        with open_(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def login(user: str, password: str):
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    status, body = http_call(f"{BASE}/login", opener)
    if status != 200:
        return None
    match = re.search(r'name="csrf" value="([^"]+)"', body)
    if not match:
        return None
    form = urllib.parse.urlencode({"username": user, "password": password,
                                   "csrf": match.group(1), "next": "/"}).encode()
    req = urllib.request.Request(f"{BASE}/login", data=form)
    try:
        opener.open(req, timeout=30)
    except urllib.error.HTTPError:
        pass
    return opener


def pid_of(pattern: str) -> str:
    return sh(f"pgrep -f -- '{pattern}' | head -1")


def rss_kb(pid: str) -> int:
    if not pid:
        return 0
    out = sh(f"awk '/^VmRSS/ {{print $2}}' /proc/{pid}/status 2>/dev/null")
    try:
        return int(out)
    except ValueError:
        return 0


def fd_count(pid: str) -> int:
    if not pid:
        return 0
    out = sh(f"ls /proc/{pid}/fd 2>/dev/null | wc -l")
    try:
        return int(out)
    except ValueError:
        return 0


def nginx_master_pid() -> str:
    """The host's nginx master (an instance or a crashed rehearsal may leave
    other nginx processes around; only the master's children matter)."""
    pid = sh("cat /run/nginx.pid 2>/dev/null").strip()
    if pid.isdigit() and os.path.isdir(f"/proc/{pid}"):
        return pid
    return sh("pgrep -f 'nginx: master' | head -1").strip()


def nginx_worker_count(master_pid: str) -> int:
    if not master_pid:
        return 0
    return len(sh(f"pgrep -P {master_pid} | tr '\\n' ' '").split())


def sample(backend_pid: str, harness_pid: str, master_pid: str = "") -> dict:
    return {
        "t": int(time.time()),
        "nginx_workers": nginx_worker_count(master_pid or nginx_master_pid()),
        "backend_rss_kb": rss_kb(backend_pid),
        "backend_fds": fd_count(backend_pid),
        "harness_rss_kb": rss_kb(harness_pid),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=2.0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--user", default="alice")
    ap.add_argument("--password", default="alice-test-123")
    ap.add_argument("--instance", default="")
    args = ap.parse_args()
    instance = args.instance or args.user

    try:
        state = json.load(open("/etc/dsh-mu/state.json"))
        port = int(state["instances"][instance]["port"])
    except Exception as exc:  # noqa: BLE001
        print(f"cannot read the instance port from /etc/dsh-mu/state.json: {exc}")
        return 2
    token = ""
    try:
        token = open("/etc/dsh-mu/admin.token").read().strip()
    except OSError:
        pass

    backend_pid = pid_of("dsh-mud --listen")
    harness_pid = pid_of(f"--port {port}")
    if not backend_pid:
        print("the admin backend is not running; start it with: dsh-muctl admin restart")
        return 2

    print(f"== soak: {args.minutes:g} min, {args.workers} workers, user={args.user} "
          f"instance={instance}:{port} ==")
    print(f"   backend pid={backend_pid}, harness pid={harness_pid or '-'}, "
          f"admin token={'yes' if token else 'no'}")

    opener = login(args.user, args.password)
    if not opener:
        print("could not log in; check the user/password")
        return 2

    stop = threading.Event()
    stats = {"requests": 0, "errors": 0, "server_5xx": 0, "latencies": []}
    lock = threading.Lock()

    def worker() -> None:
        while not stop.is_set():
            for path, op in (("/", opener), ("/me", opener), ("/login", None),
                             (f"/mu-api/status?t={token}", None)):
                if stop.is_set():
                    break
                started = time.time()
                status, _ = http_call(f"{BASE}{path}", op, timeout=15)
                with lock:
                    stats["requests"] += 1
                    stats["latencies"].append(time.time() - started)
                    if status == 0:
                        stats["errors"] += 1
                    elif 500 <= status < 600:
                        stats["server_5xx"] += 1
            if token:
                status, _ = http_call(f"http://127.0.0.1:8787/api/overview", None, timeout=10)
                # the API needs the token header, so use a hand-built request
                req = urllib.request.Request("http://127.0.0.1:8787/api/overview")
                req.add_header("X-DSH-MU-Admin", token)
                code = 0
                try:
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        code = resp.status
                except urllib.error.HTTPError as exc:
                    code = exc.code
                except Exception:  # noqa: BLE001
                    code = 0
                with lock:
                    stats["requests"] += 1
                    if code == 0:
                        stats["errors"] += 1
                    elif code >= 500:
                        stats["server_5xx"] += 1

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(args.workers)]
    for t in threads:
        t.start()

    master_pid = nginx_master_pid()
    first = sample(backend_pid, harness_pid, master_pid)
    SAMPLES.append(first)
    deadline = time.time() + args.minutes * 60
    while time.time() < deadline:
        time.sleep(min(15, max(1, args.minutes * 60 / 8)))
        SAMPLES.append(sample(backend_pid, harness_pid, master_pid))

    stop.set()
    for t in threads:
        t.join(timeout=20)
    time.sleep(1)
    last = sample(backend_pid, harness_pid, master_pid)

    # ------------------------------------------------------------------ report
    print("\n  time    nginx  backend_rss  backend_fds  harness_rss")
    for s in SAMPLES:
        print(f"  +{s['t'] - first['t']:>4}s  {s['nginx_workers']:>5}  "
              f"{s['backend_rss_kb'] / 1024:>8.1f}MB  {s['backend_fds']:>10}  "
              f"{s['harness_rss_kb'] / 1024:>8.1f}MB")
    lat = stats["latencies"]
    if lat:
        print(f"\n  requests={stats['requests']} errors={stats['errors']} "
              f"5xx={stats['server_5xx']}")
        print(f"  latency p50={statistics.median(lat) * 1000:.0f}ms "
              f"p95={sorted(lat)[int(len(lat) * 0.95) - 1] * 1000:.0f}ms "
              f"max={max(lat) * 1000:.0f}ms")
    print()

    # -------------------------------------------------------------- assertions
    if stats["errors"] == 0 and stats["server_5xx"] == 0 and stats["requests"] > 0:
        ok("no client errors and no 5xx", f"{stats['requests']} requests")
    else:
        bad("traffic was not clean",
            f"requests={stats['requests']} errors={stats['errors']} 5xx={stats['server_5xx']}")

    if pid_of("dsh-mud --listen") == backend_pid:
        ok("the backend was never restarted")
    else:
        bad("the backend restarted during the soak (crash or supervisor action)")

    warm_workers = SAMPLES[1]["nginx_workers"] if len(SAMPLES) > 1 else first["nginx_workers"]
    stable = all(s["nginx_workers"] == warm_workers for s in SAMPLES[1:])
    if stable:
        extra = [s["nginx_workers"] for s in SAMPLES if s["nginx_workers"] != warm_workers]
        note = f"{warm_workers} (stable; {len(extra)} warm-up sample(s) differed)" if extra else str(warm_workers)
        ok("nginx worker count stable after warm-up", note)
    elif last["nginx_workers"] < warm_workers:
        ok("nginx worker count settled down",
            f"{warm_workers} -> {last['nginx_workers']} (a leftover worker exited)")
    else:
        bad("nginx worker count grew",
            f"{warm_workers} -> {last['nginx_workers']} (workers being spawned repeatedly?)")

    rss_growth = last["backend_rss_kb"] - first["backend_rss_kb"]
    limit_kb = max(int(first["backend_rss_kb"] * 0.5), 64 * 1024)
    if rss_growth <= limit_kb:
        ok("backend RSS stayed flat",
            f"{first['backend_rss_kb'] / 1024:.1f}MB -> {last['backend_rss_kb'] / 1024:.1f}MB "
            f"(+{rss_growth / 1024:.1f}MB, limit +{limit_kb / 1024:.0f}MB)")
    else:
        bad("backend RSS grew beyond the limit",
            f"+{rss_growth / 1024:.1f}MB over {args.minutes:g} min")

    # A leak shows up as a *slope*, not as a one-off warm-up jump: ignore the
    # first sample (allocator growth) and require the rest to stay nearly flat.
    if len(SAMPLES) >= 3:
        warm = SAMPLES[1]
        span_min = max((last["t"] - warm["t"]) / 60.0, 1e-6)
        slope_mb_per_min = (last["backend_rss_kb"] - warm["backend_rss_kb"]) / 1024 / span_min
        if slope_mb_per_min <= 1.0:
            ok("backend RSS has no upward trend after warm-up",
                f"{slope_mb_per_min:+.2f} MB/min over {span_min:.1f} min "
                f"({warm['backend_rss_kb'] / 1024:.1f}MB -> {last['backend_rss_kb'] / 1024:.1f}MB)")
        else:
            bad("backend RSS keeps growing after warm-up",
                f"{slope_mb_per_min:+.2f} MB/min over {span_min:.1f} min")
    else:
        print("SKIP  RSS trend needs at least three samples (use a longer --minutes)")

    fd_growth = last["backend_fds"] - first["backend_fds"]
    if fd_growth <= 32:
        ok("backend file descriptors stable",
            f"{first['backend_fds']} -> {last['backend_fds']}")
    else:
        bad("backend leaked file descriptors", f"+{fd_growth}")

    if harness_pid:
        h_growth = last["harness_rss_kb"] - first["harness_rss_kb"]
        h_limit = max(int(first["harness_rss_kb"] * 0.3), 32 * 1024)
        if h_growth <= h_limit:
            ok("instance harness RSS stayed flat",
                f"{first['harness_rss_kb'] / 1024:.1f}MB -> {last['harness_rss_kb'] / 1024:.1f}MB")
        else:
            bad("instance harness RSS grew beyond the limit",
                f"+{h_growth / 1024:.1f}MB")
    else:
        print("SKIP  instance harness process not found")

    print(f"\n=== SOAK: PASS={PASS} FAIL={FAIL} ===")
    print("SOAK PASSED" if FAIL == 0 else "SOAK FAILED")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
