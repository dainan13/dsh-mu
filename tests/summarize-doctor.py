#!/usr/bin/env python3
"""Summarize a `dsh-muctl doctor --json` capture (used by fresh-rehearsal.sh).

The capture mixes stderr diagnostics with the JSON document, so the JSON is
located rather than assumed to start at byte 0.
"""
import json
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: summarize-doctor.py <capture-file>")
        return 2
    raw = open(sys.argv[1], errors="replace").read()
    start = raw.find("{")
    if start < 0:
        print("no JSON found | head:", raw[:120].replace("\n", " "))
        return 1
    try:
        data = json.loads(raw[start:])
    except json.JSONDecodeError as exc:
        print("unparsable:", exc, "| head:", raw[:120].replace("\n", " "))
        return 1
    summary = data.get("summary", {})
    failing = [c["check"] for c in data.get("checks", []) if c.get("status") == "FAIL"]
    print(summary, end="")
    if failing:
        print(" | FAIL:", ", ".join(failing[:6]), end="")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
