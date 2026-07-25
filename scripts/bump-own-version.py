#!/usr/bin/env python3
"""Bump a manifest's own patch version so a dependency-bump PR can pass the gate.

The version gate treats a Cargo.toml dependency change as release-affecting:
`require_bump` is set by `dep_manifest_changes` alone. The dependency wave
rewrites dependency pins without touching the consumer's own version, so the PR
it opens asks for a bump that nothing made, and fails a gate the automation
itself triggered.

The new version is computed from the base revision rather than from the working
tree, so re-running the wave on the same branch converges on one bump instead of
inflating the patch number every run.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys

SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(.*)$")


def own_version_span(text: str):
    """Locate the manifest's own version: [workspace.package] wins, else [package].

    Returns (start, end, value) for the version literal, or None. Only these two
    tables carry the crate's own version; a `version` under [dependencies.*] or
    [workspace.dependencies] belongs to something else and must never be touched.
    """
    section = None
    for m in re.finditer(r"^\s*\[([^\]]+)\]\s*$|^(\s*)version\s*=\s*\"([^\"]+)\"", text, re.M):
        if m.group(1) is not None:
            section = m.group(1).strip()
            continue
        if section in ("package", "workspace.package"):
            start = m.start(3)
            return start, m.end(3), m.group(3)
    return None


def bump_patch(version: str) -> str:
    m = SEMVER_RE.match(version)
    if not m:
        raise SystemExit(f"refusing to bump: '{version}' is not semver")
    major, minor, patch, suffix = m.group(1), m.group(2), int(m.group(3)), m.group(4)
    if suffix:
        raise SystemExit(f"refusing to bump prerelease/build version '{version}'")
    return f"{major}.{minor}.{patch + 1}"


def base_text(base_ref: str, path: str):
    try:
        return subprocess.run(
            ["git", "show", f"{base_ref}:{path}"],
            capture_output=True, text=True, check=True,
        ).stdout
    except subprocess.CalledProcessError:
        return None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Bump a manifest's own patch version")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--base-ref", default="origin/main")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv[1:])

    text = open(args.manifest, encoding="utf-8").read()
    span = own_version_span(text)
    if span is None:
        print(f"{args.manifest}: no [package]/[workspace.package] version; nothing to bump")
        return 0
    start, end, current = span

    # Derive the target from the base so repeated runs converge.
    base = base_text(args.base_ref, args.manifest)
    reference = current
    if base is not None:
        base_span = own_version_span(base)
        if base_span is not None:
            reference = base_span[2]

    target = bump_patch(reference)
    if current == target:
        print(f"{args.manifest}: already at {target} relative to {args.base_ref}; no change")
        return 0

    print(f"{args.manifest}: {current} -> {target} (base {reference})")
    if args.dry_run:
        return 0
    open(args.manifest, "w", encoding="utf-8").write(text[:start] + target + text[end:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
