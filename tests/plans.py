#!/usr/bin/env python3
"""Model plans: rendering, credentials, and the browser-session record.

The regression this guards: syncing a plan used to rewrite the instance's whole
`~/.dsh/.credentials.yaml`, which dropped its `records:` section (the web client's
browser-session signing secret). The harness then rejected the gateway's minted
cookie, so the instance answered 401 with no configuration error anywhere.

Usage: python3 tests/plans.py [--keep]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

CLI = "/usr/local/sbin/dsh-muctl"
INST = "/var/lib/dsh-mu/instances"
PLANS = "/var/lib/dsh-mu/plans"

PASS = FAIL = 0


def ok(msg: str) -> bool:
    global PASS
    PASS += 1
    print(f"PASS  {msg}")
    return True


def bad(msg: str) -> bool:
    global FAIL
    FAIL += 1
    print(f"FAIL  {msg}")
    return True


def sh(cmd: str, timeout: int = 120) -> tuple[int, str]:
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return res.returncode, (res.stdout or "") + (res.stderr or "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="leave the test plan in place")
    ap.add_argument("--instance", default="alice")
    ap.add_argument("--provider", default="opencode-go")
    args = ap.parse_args(argv)

    name = args.instance
    inst_home = f"{INST}/{name}/home"
    patch = f"{inst_home}/.dsh-mu-provider.yml"
    cred = f"{inst_home}/.dsh/.credentials.yaml"

    code, out = sh(f"{CLI} provider list")
    if args.provider not in out:
        print(f"SKIP  provider {args.provider} is not in the catalog")
        return 0

    # ------------------------------------------------ 1. plant a records: block
    code, out = sh(f"test -f {cred} && echo yes")
    if "yes" not in out:
        print(f"SKIP  {name} has no credential store yet (start the instance once)")
        return 0
    text = open(cred).read()
    planted = ""
    if "records:" not in text:
        planted = ("records:\n  client-connection/browser-session:\n    kind: secret\n"
                   "    payload:\n      version: 1\n      secret: dshmu-test-secret\n")
        with open(cred, "a") as fh:
            fh.write(planted)
    ok("planted a records: (browser-session secret) in the credential store")

    # --------------------------------------------------------------- 2. add a plan
    # Pick models that the package actually has (the catalogs change over time).
    code, out = sh(f"{CLI} provider export {args.provider}")
    try:
        pkg = json.loads(out[out.index("{"):])
        models = pkg.get("models") or []
    except (ValueError, json.JSONDecodeError):
        models = []
    if not models:
        print(f"SKIP  could not read the model list of {args.provider}")
        return 0
    picks = models[0]["id"]
    extra = next((m["id"] for m in models if (m.get("api") or pkg.get("api")) != "openai-completions"), None)
    if extra and extra != picks:
        picks = f"{picks},{extra}"

    plan_name = "dsh-mu plan test"
    with open("/tmp/dshmu-plan-test.key", "w") as fh:
        fh.write("sk-plan-test-not-a-real-key\n")
    os.chmod("/tmp/dshmu-plan-test.key", 0o600)
    code, out = sh(f"{CLI} plan rm plan_{__import__('hashlib').sha256(plan_name.encode()).hexdigest()[:8]}",
                   timeout=60)
    code, out = sh(f"{CLI} plan add --name '{plan_name}' --provider {args.provider} "
                   f"--key-file /tmp/dshmu-plan-test.key --models {picks}",
                   timeout=180)
    if code != 0:
        bad(f"plan add failed: {out.strip()[:200]}")
        return 1
    ok("plan add reported success")

    pid = f"plan_{__import__('hashlib').sha256(plan_name.encode()).hexdigest()[:8]}"
    code, out = sh(f"{CLI} plan list")
    pid in out and ok("plan appears in `plan list`") or bad("plan missing from `plan list`")

    # ------------------------------------------------- 3. patch and credential refs
    body = open(patch).read() if os.path.exists(patch) else ""
    # A plan whose provider spans several base URLs renders one route per base URL,
    # each suffixed with the protocol, so compare the prefix.
    f'displayName: "{plan_name}' in body and ok("patch carries the plan name as displayName") \
        or bad("patch has no displayName for the plan")
    f"apiKeyEnv: {('DSHMU_' + pid.upper().replace('-', '_'))}" in body \
        and ok("patch references the plan credential") \
        or bad("patch does not reference the plan credential")
    if extra:
        f"api: {next(m.get('api') for m in models if m['id'] == extra)}" in body \
            and ok("per-model protocol override rendered") \
            or bad("per-model protocol override missing")
    else:
        ok("provider has a single protocol; no per-model override expected")

    store = open(cred).read()
    re.search(r"^\s+DSHMU_PLAN_[A-Z0-9_]+:", store, re.M) \
        and ok("plan credential stored in refs:") \
        or bad("plan credential missing from refs:")
    "records:" in store and ok("records: survived the plan sync (the 401 regression)") \
        or bad("records: was dropped by the plan sync -- the instance will answer 401")

    # ------------------------------------------------------------------ 4. cleanup
    if not args.keep:
        sh(f"{CLI} plan rm {pid}", timeout=120)
        after = open(cred).read()
        "records:" in after and ok("records: still there after removing the plan") \
            or bad("removing the plan dropped records:")
        # Only *this* plan's ref must be gone; other visible plans keep theirs.
        own = "DSHMU_" + pid.upper().replace("-", "_")
        own in after and bad(f"ref {own} left behind after removal") \
            or ok("this plan's ref was removed from the credential store")

    # The web_search tool has its own endpoint+credential. Left unconfigured it reads
    # DEEPSEEK_API_KEY (the relay placeholder) and calls api.deepseek.com directly,
    # which is the 401 "your api key ****elay is invalid" report. The patch must
    # always carry an explicit web-search block (configured, or explicitly disabled).
    with open(patch, encoding="utf-8") as fh:
        patch_text = fh.read()
    block = patch_text.split("- id: web-search-deepseek", 1)
    if len(block) == 2:
        tail = block[1].split("\n- id: ", 1)[0]
        if "DSHMU_WEB_SEARCH_UNCONFIGURED" in tail:
            ok("web search is registered but unavailable (no DeepSeek plan yet)")
        elif "disabled: true" in tail:
            ok("web search is explicitly disabled (no plan can serve it)")
        elif "baseURL:" in tail and "apiKeyEnv:" in tail:
            line = [l.strip() for l in tail.splitlines() if "baseURL:" in l][0]
            ok("web search points at its own endpoint with its own credential")
        else:
            bad("the web-search block is neither configured nor disabled")
    else:
        bad("the patch has no web-search-deepseek block (searches would 401 on the relay placeholder)")

    print(f"\n=== PLANS: PASS={PASS} FAIL={FAIL} ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
