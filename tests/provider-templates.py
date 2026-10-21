#!/usr/bin/env python3
"""Provider templates: list / fetch / validate / import, and the guards.

Runs fully offline: the template source is pointed at a temporary file:// directory
so no network and no GitHub rate limits are involved.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

CLI = os.environ.get("DSH_MU_CLI", "/usr/local/sbin/dsh-muctl")
PASS = 0
FAIL = 0


def ok(msg, detail=""):
    global PASS
    PASS += 1
    print(f"PASS  {msg}" + (f"  -- {detail}" if detail else ""))


def bad(msg, detail=""):
    global FAIL
    FAIL += 1
    print(f"FAIL  {msg}" + (f"  -- {detail}" if detail else ""))


def sh(cmd, check=False):
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise SystemExit(f"command failed: {cmd}\n{proc.stdout}\n{proc.stderr}")
    return proc.returncode, proc.stdout + proc.stderr


def main():
    src = tempfile.mkdtemp(prefix="tpl-src-")
    pkg_id = "tplcheck"
    # Idempotent: a previous aborted run must not turn this into "already exists".
    sh(f"{CLI} provider rm {pkg_id}")
    try:
        # a valid template + index
        pkg = {
            "schema": "dsh-mu.provider/v1", "id": pkg_id, "label": "Template Check",
            "base_url": "https://example.invalid/v1", "api": "openai-completions",
            "default_model": "m1",
            "models": [{"id": "m1", "name": "M1"}],
        }
        with open(os.path.join(src, f"{pkg_id}.json"), "w") as fh:
            json.dump(pkg, fh)
        with open(os.path.join(src, "index.json"), "w") as fh:
            json.dump({"version": 1, "templates": [
                {"name": pkg_id, "label": "Template Check", "api": "openai-completions",
                 "description": "a test template", "models": 1}]}, fh)
        # a template that must be rejected
        with open(os.path.join(src, "broken.json"), "w") as fh:
            json.dump({"schema": "dsh-mu.provider/v1", "id": "broken", "models": []}, fh)

        code, out = sh(f"{CLI} config provider_template_base file://{src}")
        if code == 0 and "provider_template_base" in out:
            ok("the template source is configurable", out.strip().splitlines()[-1])
        else:
            bad("could not set provider_template_base", out[:120])

        code, out = sh(f"{CLI} template list")
        if code == 0 and pkg_id in out:
            ok("template list shows the published templates", out.strip().splitlines()[1][:80])
        else:
            bad("template list did not show the template", out[:160])

        code, out = sh(f"{CLI} template import {pkg_id}")
        if code == 0 and "sha256" in out:
            ok("a template imports and reports its sha256",
               [l for l in out.splitlines() if l.startswith("sha256")][:1])
        else:
            bad("template import failed", out[:200])

        # `provider show` only knows presets; an imported package is read with export.
        code, out = sh(f"{CLI} provider export {pkg_id}")
        if code == 0 and '"m1"' in out:
            ok("the imported package is readable through provider export")
        else:
            bad("imported package is not readable", out[:160])

        code, out = sh(f"{CLI} template import {pkg_id}")
        if code != 0 and "already exists" in out:
            ok("importing twice without --force is refused", out.strip().splitlines()[-1][:80])
        else:
            bad("re-import without --force was allowed", out[:160])

        code, out = sh(f"{CLI} template import broken")
        if code != 0 and ("validation" in out or "failed" in out):
            ok("an invalid template is rejected before it is saved", out.strip().splitlines()[-1][:90])
        else:
            bad("an invalid template was accepted", out[:160])

        code, out = sh(f"{CLI} template import ../etc/passwd")
        if code != 0:
            ok("path traversal in a template name is refused")
        else:
            bad("path traversal in a template name was accepted")

        code, out = sh(f"{CLI} template list")
        if "broken" not in out or code != 0:
            ok("rejected templates do not appear in the provider list")
        else:
            bad("a rejected template appeared in the provider list")
    finally:
        sh(f"{CLI} provider rm {pkg_id}")
        sh(f"{CLI} config provider_template_base https://raw.githubusercontent.com/dainan13/dsh-mu/main/provider-packages")
        shutil.rmtree(src, ignore_errors=True)

    code, out = sh(f"{CLI} doctor")
    line = next((l for l in out.splitlines() if l.startswith("doctor:")), "")
    if "0 FAIL" in line:
        ok("doctor is clean after the drill", line)
    else:
        bad("doctor is not clean", line or "no doctor line")

    print(f"\n=== PROVIDER TEMPLATES: PASS={PASS} FAIL={FAIL} ===")
    print("PROVIDER TEMPLATES PASSED" if FAIL == 0 else "PROVIDER TEMPLATES FAILED")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
