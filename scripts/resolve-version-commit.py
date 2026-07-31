#!/usr/bin/env python3
"""Resolve the exact first-parent commit that introduced a version.

Release controllers may run after unrelated commits have landed on ``main``.
Tagging the latest head would silently include those later bytes in the prior
version. This helper walks the contiguous first-parent suffix carrying the
current version and returns its oldest commit: the commit that introduced that
version on the protected branch.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path


FULL_OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


class VersionCommitError(RuntimeError):
    """Version-introducing commit authority could not be established."""


def _load_cargo_prepare():
    path = Path(__file__).with_name("prepare-cargo-release.py")
    spec = importlib.util.spec_from_file_location("version_commit_cargo", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cargo_prepare = _load_cargo_prepare()


def _run(
    args: list[str],
    *,
    root: Path,
    check: bool = True,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        args,
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise VersionCommitError(
            f"{' '.join(args)} failed: {detail or result.returncode}"
        )
    return result


def text_version_at_ref(root: Path, ref: str, version_file: str) -> str:
    result = _run(
        ["git", "--no-replace-objects", "show", f"{ref}:{version_file}"],
        root=root,
        check=False,
    )
    if result.returncode != 0:
        raise VersionCommitError(
            f"could not read {version_file} at {ref}"
        )
    return result.stdout.strip()


def first_parent_commits(root: Path, head_ref: str) -> list[str]:
    commits = _run(
        ["git", "--no-replace-objects", "rev-list", "--first-parent", head_ref],
        root=root,
    ).stdout.splitlines()
    if not commits or any(not FULL_OID_RE.fullmatch(item) for item in commits):
        raise VersionCommitError(
            f"could not resolve exact first-parent history from {head_ref}"
        )
    return commits


def resolve_version_commit(
    *,
    root: Path,
    head_ref: str,
    target_version: str,
    version_file: str | None = None,
    cargo_manifest: str | None = None,
    version_reader=None,
) -> str:
    """Return the oldest commit in HEAD's contiguous target-version suffix."""

    root = root.resolve()
    sources = sum(
        item is not None
        for item in (version_file, cargo_manifest, version_reader)
    )
    if sources != 1:
        raise VersionCommitError(
            "exactly one version source must be selected"
        )
    if version_reader is not None:
        read_version = version_reader
    elif version_file is not None:
        read_version = lambda ref: text_version_at_ref(
            root, ref, version_file
        )
    else:
        read_version = lambda ref: cargo_prepare.version_at_ref(
            root, ref, cargo_manifest
        )
    commits = first_parent_commits(root, head_ref)
    introduced = ""
    for index, commit in enumerate(commits):
        try:
            version = read_version(commit)
        except (VersionCommitError, cargo_prepare.ReleasePreparationError):
            if index == 0:
                raise
            break
        if version != target_version:
            if index == 0:
                raise VersionCommitError(
                    f"{head_ref} carries version {version}, expected "
                    f"{target_version}"
                )
            break
        introduced = commit
    if not introduced:
        raise VersionCommitError(
            f"no first-parent commit introduces version {target_version}"
        )
    return introduced


def _write_outputs(version: str, commit: str) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return
    with open(output, "a", encoding="utf-8") as stream:
        stream.write(f"version={version}\n")
        stream.write(f"version_commit={commit}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--head-ref", default="HEAD")
    parser.add_argument("--target-version", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--version-file")
    source.add_argument("--cargo-manifest")
    args = parser.parse_args(argv)

    try:
        root = args.root.resolve()
        commit = resolve_version_commit(
            root=root,
            head_ref=args.head_ref,
            target_version=args.target_version,
            version_file=args.version_file,
            cargo_manifest=args.cargo_manifest,
        )
    except (OSError, VersionCommitError, cargo_prepare.ReleasePreparationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    _write_outputs(args.target_version, commit)
    print(commit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
