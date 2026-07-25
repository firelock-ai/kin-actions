#!/usr/bin/env python3
"""Reject workflow files that read a context unavailable where they read it.

GitHub validates context availability when it loads a workflow, and a violation
fails the run before any job starts, with no job-level log to read. That failure
mode is expensive: it looks like a broken release rather than a typo, and it only
appears once the workflow is pushed and triggered.

`secrets` is the one that bites. It is available in `env`, `with`, and `run`, but
NOT in an `if:` condition. Resolve presence into `env` first, then branch on that.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Contexts that GitHub does not expose to `if:` expressions.
FORBIDDEN_IN_IF = ("secrets",)


def violations(path: Path) -> list[tuple[int, str, str]]:
    found: list[tuple[int, str, str]] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if line.startswith("#"):
            continue
        # `if:` as a mapping key, not the word appearing inside a script body.
        if not re.match(r"^-?\s*if\s*:", line):
            continue
        for ctx in FORBIDDEN_IN_IF:
            if re.search(rf"\b{ctx}\s*\.", line):
                found.append((lineno, ctx, line))
    return found


def main(argv: list[str]) -> int:
    roots = [Path(a) for a in argv[1:]] or [Path(".github/workflows")]
    files: list[Path] = []
    for r in roots:
        files.extend(sorted(r.rglob("*.yml")) if r.is_dir() else [r])

    bad = 0
    for f in files:
        for lineno, ctx, line in violations(f):
            bad += 1
            print(f"{f}:{lineno}: `{ctx}` is not available in an `if:` condition")
            print(f"    {line}")
            print(f"    fix: set `env: HAVE_X: ${{{{ {ctx}.NAME != '' }}}}` on the job, then `if: env.HAVE_X == 'true'`")
    if bad:
        print(f"\n{bad} context violation(s); GitHub would reject these workflow files at load time.")
        return 1
    print(f"checked {len(files)} workflow file(s): no forbidden contexts in `if:` conditions")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
