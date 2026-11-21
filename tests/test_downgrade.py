#!/usr/bin/env python3
"""Downgrade check: update_all.py and web/zmod_update_all.js must agree.
Run: python3 tests/test_downgrade.py (needs node for the JS half)."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
spec = importlib.util.spec_from_file_location(
    "update_all", os.path.join(ROOT, "update_all.py"))
ua = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ua)

# (installed, offered, type, is a downgrade)
CASES = [
    ("v1.1.0-rc.4", "v1.0.11", "web", True),    # the C5/AD5X-1 case
    ("v1.0.11", "v1.1.0-rc.4", "web", False),
    ("v1.1.0-rc.4", "v1.1.0", "web", False),    # release > its rc
    ("v1.1.0", "v1.1.0-rc.4", "web", True),
    ("v1.1.0-rc.3", "v1.1.0-rc.4", "web", False),
    ("v1.1.0-rc.10", "v1.1.0-rc.9", "web", True),  # numeric, not text
    ("v1.1.0-beta.17", "v1.1.0-rc.1", "web", False),
    ("v0.1.12", "v0.1.21", "web", False),
    ("v2.19.6", "v2.19.6", "web", False),
    ("v1.38.0", "v1.37.18", "web", True),
    ("1.2", "1.10", "web", False),
    ("?", "v1.0.0", "web", False),              # unknown: never skip
    ("v1.0.0", "nightly", "web", False),
    ("1.7.6-85", "1.7.6-78", "git_repo", False),  # git never counts
]


def main():
    bad = 0
    for have, offer, typ, want in CASES:
        info = {"configured_type": typ, "version": have,
                "remote_version": offer}
        got = ua.downgrade(info)
        if got != want:
            bad += 1
            print(f"FAIL py  {have} -> {offer} ({typ}): {got}, want {want}")
    node = shutil.which("node")
    if not node:
        print("node not found, JS half skipped")
    else:
        js = open(os.path.join(ROOT, "web", "zmod_update_all.js"),
                  encoding="utf-8").read()
        # Pull the helpers out of the IIFE and run them on the same cases.
        start = js.index("  function verKey")
        end = js.index("  // Same rule as update_all.py")
        prog = js[start:end] + (
            "var cases = " + json.dumps(CASES) + ";\n"
            "process.stdout.write(JSON.stringify(cases.map(function (c) {\n"
            "  return downgrade({configured_type: c[2], version: c[0],\n"
            "                    remote_version: c[1]}) })))\n")
        out = subprocess.run([node, "-e", prog], capture_output=True,
                             text=True, check=True).stdout
        for (have, offer, typ, want), got in zip(CASES, json.loads(out)):
            if got != want:
                bad += 1
                print(f"FAIL js  {have} -> {offer} ({typ}): {got}, want {want}")
    total = len(CASES) * (2 if node else 1)
    print(f"{total - bad}/{total} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
