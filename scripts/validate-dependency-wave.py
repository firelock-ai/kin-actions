#!/usr/bin/env python3
"""Admit only the exact generated dependency-wave delta and stage it."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath
from typing import Sequence


class AdmissionError(RuntimeError):
    """The dependency worktree is not an exact generated candidate."""


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _paths(output: bytes) -> set[str]:
    return {
        os.fsdecode(value)
        for value in output.split(b"\0")
        if value
    }


def normalize_path(raw: str) -> str:
    path = PurePosixPath(raw)
    if path.is_absolute() or not raw or raw.endswith("/"):
        raise AdmissionError(f"invalid generated path: {raw!r}")
    if any(part in ("", ".", "..") for part in path.parts):
        raise AdmissionError(f"generated path must be normalized: {raw!r}")
    return path.as_posix()


def tracked_at_head(path: str) -> bool:
    return git("cat-file", "-e", f"HEAD:{path}", check=False).returncode == 0


def _document(content: bytes, path: str) -> dict[str, object]:
    try:
        value = tomllib.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise AdmissionError(f"cannot parse {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AdmissionError(f"{path} is not a TOML object")
    return value


def _version_fields(document: dict[str, object]) -> tuple[object, object]:
    package = document.get("package")
    package_version: object = None
    if isinstance(package, dict):
        package_version = package.get("version")

    workspace = document.get("workspace")
    workspace_version: object = None
    if isinstance(workspace, dict):
        workspace_package = workspace.get("package")
        if isinstance(workspace_package, dict):
            workspace_version = workspace_package.get("version")
    return package_version, workspace_version


def version_changed(path: str) -> bool:
    baseline = git("show", f"HEAD:{path}").stdout
    try:
        current = Path(path).read_bytes()
    except OSError as exc:
        raise AdmissionError(f"cannot read generated path {path}: {exc}") from exc
    return _version_fields(_document(baseline, path)) != _version_fields(
        _document(current, path)
    )


def admit(
    manifests: Sequence[str],
    *,
    version_mode: str,
    bump_own_version: bool,
    ephemeral_paths: Sequence[str] = (),
    expected_tree: str | None = None,
) -> dict[str, object]:
    if version_mode not in {"manual", "train"}:
        raise AdmissionError("version mode must be manual or train")
    if version_mode == "train" and bump_own_version:
        raise AdmissionError("train mode cannot admit an own-version bump")
    if expected_tree is not None and (
        len(expected_tree) not in (40, 64)
        or any(char not in "0123456789abcdef" for char in expected_tree)
    ):
        raise AdmissionError(f"invalid expected tree object ID: {expected_tree!r}")

    manifest_paths = tuple(dict.fromkeys(normalize_path(value) for value in manifests))
    if not manifest_paths:
        raise AdmissionError("at least one manifest is required")
    for path in manifest_paths:
        candidate = Path(path)
        if (
            not tracked_at_head(path)
            or not candidate.is_file()
            or candidate.is_symlink()
        ):
            raise AdmissionError(f"manifest must be a tracked regular file: {path}")

    allowed = set(manifest_paths)
    if tracked_at_head("Cargo.lock"):
        allowed.add("Cargo.lock")
    if version_mode == "manual" and bump_own_version and tracked_at_head("Cargo.toml"):
        allowed.add("Cargo.toml")

    changed = _paths(git("diff", "--name-only", "-z", "HEAD", "--").stdout)
    untracked = _paths(
        git("ls-files", "--others", "--exclude-standard", "-z").stdout
    )
    ephemeral = tuple(
        normalize_path(value.rstrip("/")) + "/" for value in ephemeral_paths
    )
    unexpected_untracked = sorted(
        path
        for path in untracked
        if not any(path == prefix[:-1] or path.startswith(prefix) for prefix in ephemeral)
    )
    if unexpected_untracked:
        raise AdmissionError(
            "unexpected untracked paths: " + ", ".join(unexpected_untracked)
        )

    unexpected = sorted(changed - allowed)
    if unexpected:
        raise AdmissionError(
            "dependency wave changed non-allowlisted paths: "
            + ", ".join(unexpected)
        )
    if not changed:
        raise AdmissionError("dependency updater reported a change but the tree is clean")
    structural = git("diff", "--summary", "HEAD", "--").stdout.decode(
        "utf-8", errors="replace"
    ).strip()
    if structural:
        raise AdmissionError(
            "dependency wave changed file identity or mode: " + structural
        )

    version_paths = set(manifest_paths)
    if tracked_at_head("Cargo.toml"):
        version_paths.add("Cargo.toml")
    changed_versions = sorted(
        path
        for path in version_paths
        if path in changed and version_changed(path)
    )
    if changed_versions and (version_mode == "train" or not bump_own_version):
        raise AdmissionError(
            "own-version authority changed without authorization: "
            + ", ".join(changed_versions)
        )

    ordered = sorted(changed)
    git("add", "--", *ordered)
    staged = _paths(git("diff", "--cached", "--name-only", "-z").stdout)
    if staged != changed:
        raise AdmissionError(
            "staged dependency delta differs from admitted worktree delta"
        )
    unstaged = _paths(git("diff", "--name-only", "-z").stdout)
    if unstaged:
        raise AdmissionError(
            "dependency wave left unstaged tracked changes: "
            + ", ".join(sorted(unstaged))
        )
    tree = git("write-tree").stdout.decode("ascii").strip()
    if len(tree) not in (40, 64) or any(char not in "0123456789abcdef" for char in tree):
        raise AdmissionError(f"git write-tree returned invalid object ID: {tree!r}")
    if expected_tree is not None and tree != expected_tree:
        raise AdmissionError(
            f"verified tree {tree} differs from generated tree {expected_tree}"
        )
    return {"paths": ordered, "tree": tree}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", action="append", default=[])
    parser.add_argument("--version-mode", required=True)
    parser.add_argument(
        "--bump-own-version",
        choices=("true", "false"),
        required=True,
    )
    parser.add_argument("--ephemeral-path", action="append", default=[])
    parser.add_argument("--expected-tree")
    args = parser.parse_args(argv)
    try:
        result = admit(
            args.manifest,
            version_mode=args.version_mode,
            bump_own_version=args.bump_own_version == "true",
            ephemeral_paths=args.ephemeral_path,
            expected_tree=args.expected_tree,
        )
    except AdmissionError as exc:
        print(f"dependency-wave admission refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
