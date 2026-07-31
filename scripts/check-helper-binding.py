#!/usr/bin/env python3
"""Verify reusable workflows bind helpers to their own immutable source."""

from __future__ import annotations

import re
import sys
from pathlib import Path


REPOSITORY_BINDING = "repository: ${{ job.workflow_repository }}"
SHA_BINDING = "ref: ${{ job.workflow_sha }}"
MUTABLE_REF = "ref: ${{ inputs.kin-actions-ref }}"
CHECKOUT_USE = re.compile(r"uses:\s*actions/checkout@[^\s#]+(?:\s+#\s+.+)?")
REPOSITORY_KEY = re.compile(r"(?m)^\s*repository:\s*\S")


def checkout_blocks(text: str) -> list[str]:
    """Return every checkout step that names a repository to check out.

    A step is located from its `uses:` line and then widened back to the list
    marker that opens it, so a step declaring `name:` or `id:` before `uses:`
    is still seen. Any checkout carrying a `repository:` key is checking out
    something other than the caller itself and must therefore bind to the
    exact called-workflow source.
    """

    lines = text.splitlines()
    blocks: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("- "):
            stripped = stripped[2:].strip()
        if not CHECKOUT_USE.fullmatch(stripped):
            continue
        indent = len(line) - len(line.lstrip())
        start = index
        for back in range(index, -1, -1):
            candidate = lines[back]
            if not candidate.strip():
                continue
            back_indent = len(candidate) - len(candidate.lstrip())
            if back_indent > indent:
                continue
            if candidate.lstrip().startswith("- "):
                start = back
                break
            if back_indent < indent:
                break
        step_indent = len(lines[start]) - len(lines[start].lstrip())
        end = start + 1
        while end < len(lines):
            candidate = lines[end]
            if (
                candidate.strip()
                and len(candidate) - len(candidate.lstrip()) <= step_indent
            ):
                break
            end += 1
        block = "\n".join(lines[start:end])
        if REPOSITORY_KEY.search(block):
            blocks.append(block)
    return blocks


def violations(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    errors: list[str] = []
    if MUTABLE_REF in text:
        errors.append("uses deprecated kin-actions-ref as helper authority")
    for number, block in enumerate(checkout_blocks(text), 1):
        if REPOSITORY_BINDING not in block:
            errors.append(
                f"helper checkout {number} does not use job.workflow_repository"
            )
        if SHA_BINDING not in block:
            errors.append(f"helper checkout {number} does not use job.workflow_sha")
        if re.search(r"(?m)^\s*ref:\s*(?:main|master|v\d)", block):
            errors.append(f"helper checkout {number} uses a separately mutable ref")
    return errors


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    root = Path(args[0]) if args else Path(".")
    files = sorted((root / ".github" / "workflows").glob("*.yml"))
    checked = 0
    failures = 0
    for path in files:
        blocks = checkout_blocks(path.read_text(encoding="utf-8"))
        if not blocks and MUTABLE_REF not in path.read_text(encoding="utf-8"):
            continue
        checked += len(blocks)
        for message in violations(path):
            failures += 1
            print(f"{path}: {message}")
    if checked == 0:
        print("no reusable workflow helper checkouts found", file=sys.stderr)
        return 1
    if failures:
        print(f"{failures} helper binding violation(s)", file=sys.stderr)
        return 1
    print(
        f"checked {checked} helper checkout(s): exact called-workflow source binding"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
