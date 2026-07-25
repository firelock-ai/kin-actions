#!/usr/bin/env python3
"""Assert every workflow's kin-actions-ref default matches the VERSION file.

Reusable workflows check out their own helper scripts at `kin-actions-ref`,
defaulting to the tag they ship in. That default is easy to miss on release:
v0.1.14 shipped the dependency wave calling a helper script that only exists
from v0.1.14 on, while the wave's own default still said v0.1.8 — so every
caller checked out a helper tree without the script and the new step failed
or silently skipped. Two files must move in lockstep with VERSION; this makes
the release fail here instead of in a consumer's run.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(".")
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    want = f"v{version}"
    bad = 0
    files = sorted((root / ".github" / "workflows").glob("*.yml"))
    checked = 0
    for f in files:
        text = f.read_text(encoding="utf-8")
        m = re.search(r"kin-actions-ref:\n(?:.*\n)*?\s+default:\s*(\S+)", text)
        if not m:
            continue
        checked += 1
        got = m.group(1)
        if got != want:
            bad += 1
            print(f"{f}: kin-actions-ref default is {got}, VERSION says {want}")
    if checked == 0:
        print("no workflow declares kin-actions-ref; nothing to check")
        return 0
    if bad:
        print(f"\n{bad} default(s) out of lockstep with VERSION={version}; "
              "bump them together or callers check out mismatched helper scripts.")
        return 1
    print(f"checked {checked} workflow(s): kin-actions-ref defaults match VERSION {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
