#!/usr/bin/env python3
"""Fail-closed invariants for advancing an open Cargo release train.

The general release App intentionally has ``Contents`` permission only.  When
``main`` changes a workflow while a release-train branch is open, the workflow
must therefore let GitHub create the merge commit server-side instead of
rewriting workflow files through the Contents API.

This helper owns the local, testable parts of that protocol:

``neutralize``
    Prove that the train changed only an exact allowlist of generated files
    since its merge base, then stage those files at the exact bytes and modes
    from a trusted ``main`` commit.  The caller commits and normally pushes
    that neutralization before requesting ``POST /repos/{owner}/{repo}/merges``.

``validate-merge``
    Validate the commit returned by that API before any generated files are
    rebuilt.  The merge must have exactly two ordered parents (the neutralized
    train head, then trusted main), and its complete tree must equal trusted
    main byte-for-byte and mode-for-mode.

There are deliberately no GitHub calls here.  Authority for selecting the
trusted SHAs and invoking the API remains visible in the workflow.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Sequence


class InvariantError(RuntimeError):
    """A release-branch safety invariant was not satisfied."""


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    object_type: str
    oid: str


Tree = dict[str, TreeEntry]


def _git(
    repo: Path,
    *args: str,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess:
    command = ["git", "-C", os.fspath(repo), "--no-replace-objects", *args]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=text,
    )
    if check and result.returncode != 0:
        stderr = result.stderr if text else result.stderr.decode("utf-8", "replace")
        detail = stderr.strip() or f"exit status {result.returncode}"
        raise InvariantError(f"git {' '.join(args)} failed: {detail}")
    return result


def repository_root(repo: str | os.PathLike[str]) -> Path:
    candidate = Path(repo).resolve()
    result = _git(candidate, "rev-parse", "--show-toplevel")
    root = Path(result.stdout.strip()).resolve()
    if not root.is_dir():
        raise InvariantError(f"repository root is not a directory: {root}")
    return root


def _object_id_length(repo: Path) -> int:
    object_format = _git(repo, "rev-parse", "--show-object-format").stdout.strip()
    lengths = {"sha1": 40, "sha256": 64}
    try:
        return lengths[object_format]
    except KeyError as exc:
        raise InvariantError(f"unsupported Git object format: {object_format!r}") from exc


def validate_commit(repo: Path, value: str, label: str) -> str:
    length = _object_id_length(repo)
    if not re.fullmatch(rf"[0-9a-f]{{{length}}}", value or ""):
        raise InvariantError(
            f"{label} must be a full lowercase {length}-character object ID"
        )
    result = _git(repo, "cat-file", "-t", value, check=False)
    if result.returncode != 0:
        raise InvariantError(f"{label} commit is unavailable: {value}")
    if result.stdout.strip() != "commit":
        raise InvariantError(f"{label} is not a commit object: {value}")
    return value


def current_head(repo: Path) -> str:
    value = _git(repo, "rev-parse", "--verify", "HEAD").stdout.strip()
    return validate_commit(repo, value, "HEAD")


def validate_generated_paths(paths: Iterable[str]) -> tuple[str, ...]:
    validated: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        if not raw or raw != raw.strip():
            raise InvariantError("generated paths must be non-empty and unpadded")
        if "\\" in raw or any(ord(char) < 32 or ord(char) == 127 for char in raw):
            raise InvariantError(f"unsafe generated path: {raw!r}")
        path = PurePosixPath(raw)
        if path.is_absolute() or str(path) != raw:
            raise InvariantError(f"generated path must be normalized and relative: {raw!r}")
        if any(part in {"", ".", ".."} for part in path.parts):
            raise InvariantError(f"generated path contains an unsafe segment: {raw!r}")
        if path.parts[0] == ".git":
            raise InvariantError(f"Git metadata cannot be generated: {raw!r}")
        if path.parts[:2] == (".github", "workflows"):
            raise InvariantError(
                f"workflow files cannot be release-train generated paths: {raw!r}"
            )
        if raw in seen:
            raise InvariantError(f"duplicate generated path: {raw}")
        seen.add(raw)
        validated.append(raw)
    if not validated:
        raise InvariantError("at least one generated path is required")
    return tuple(sorted(validated))


def assert_clean_worktree(repo: Path) -> None:
    result = _git(
        repo,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        text=False,
    )
    if result.stdout:
        entries = [
            item.decode("utf-8", "backslashreplace")
            for item in result.stdout.split(b"\0")
            if item
        ]
        raise InvariantError(
            "worktree must be clean, including untracked files: " + ", ".join(entries)
        )


def _assert_worktree_matches_index(repo: Path) -> None:
    tracked = _git(
        repo,
        "diff-files",
        "--quiet",
        "--ignore-submodules=none",
        check=False,
    )
    if tracked.returncode != 0:
        raise InvariantError("neutralization left unstaged tracked changes")
    untracked = _git(
        repo,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        text=False,
    )
    if untracked.stdout:
        raise InvariantError("neutralization left untracked files")


def tree_entries(repo: Path, treeish: str) -> Tree:
    result = _git(
        repo,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        treeish,
        text=False,
    )
    entries: Tree = {}
    oid_length = _object_id_length(repo)
    header_pattern = re.compile(
        rb"(?P<mode>[0-9]{6}) (?P<type>[a-z]+) "
        + rb"(?P<oid>[0-9a-f]{"
        + str(oid_length).encode("ascii")
        + rb"})"
    )
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            path = raw_path.decode("utf-8", "strict")
        except (ValueError, UnicodeDecodeError) as exc:
            raise InvariantError(f"malformed tree entry in {treeish}") from exc
        match = header_pattern.fullmatch(header)
        if not match:
            raise InvariantError(f"malformed tree header in {treeish}: {header!r}")
        if path in entries:
            raise InvariantError(f"duplicate tree path in {treeish}: {path!r}")
        entries[path] = TreeEntry(
            mode=match.group("mode").decode("ascii"),
            object_type=match.group("type").decode("ascii"),
            oid=match.group("oid").decode("ascii"),
        )
    return entries


def _assert_safe_tree(entries: Mapping[str, TreeEntry], label: str) -> None:
    for path, entry in entries.items():
        if entry.mode == "120000":
            raise InvariantError(f"{label} contains a symlink: {path}")
        if entry.object_type != "blob":
            raise InvariantError(
                f"{label} contains unsupported {entry.object_type} entry: {path}"
            )


def changed_paths(left: Mapping[str, TreeEntry], right: Mapping[str, TreeEntry]) -> set[str]:
    return {
        path
        for path in left.keys() | right.keys()
        if left.get(path) != right.get(path)
    }


def _single_merge_base(repo: Path, left: str, right: str) -> str:
    result = _git(repo, "merge-base", "--all", left, right, check=False)
    bases = [line for line in result.stdout.splitlines() if line]
    if result.returncode != 0 or len(bases) != 1:
        raise InvariantError(
            "train and trusted main must have exactly one available merge base"
        )
    return validate_commit(repo, bases[0], "merge base")


def _require_allowlisted_train_delta(
    repo: Path,
    train_head: str,
    trusted_main: str,
    generated: Sequence[str],
    *,
    require_delta: bool,
) -> tuple[str, Tree, Tree, Tree]:
    merge_base = _single_merge_base(repo, train_head, trusted_main)
    base_tree = tree_entries(repo, merge_base)
    train_tree = tree_entries(repo, train_head)
    main_tree = tree_entries(repo, trusted_main)
    _assert_safe_tree(base_tree, "merge-base tree")
    _assert_safe_tree(train_tree, "train tree")
    _assert_safe_tree(main_tree, "trusted-main tree")

    known = base_tree.keys() | train_tree.keys() | main_tree.keys()
    absent = sorted(set(generated) - known)
    if absent:
        raise InvariantError(
            "generated allowlist contains paths absent from all trusted trees: "
            + ", ".join(absent)
        )

    delta = changed_paths(base_tree, train_tree)
    extras = sorted(delta - set(generated))
    if extras:
        raise InvariantError(
            "train changed non-generated paths since its merge base: "
            + ", ".join(extras)
        )
    if require_delta and not delta:
        raise InvariantError("train has no generated delta to reconcile")
    return merge_base, base_tree, train_tree, main_tree


def _tree_with_main_generated(
    train_tree: Mapping[str, TreeEntry],
    main_tree: Mapping[str, TreeEntry],
    generated: Sequence[str],
) -> Tree:
    expected = dict(train_tree)
    for path in generated:
        if path in main_tree:
            expected[path] = main_tree[path]
        else:
            expected.pop(path, None)
    return expected


def neutralize(
    repo: str | os.PathLike[str],
    *,
    trusted_main: str,
    old_train_head: str,
    generated_paths: Iterable[str],
) -> dict[str, object]:
    root = repository_root(repo)
    generated = validate_generated_paths(generated_paths)
    main = validate_commit(root, trusted_main, "trusted main")
    train = validate_commit(root, old_train_head, "old train head")
    if main == train:
        raise InvariantError("trusted main and old train head must be distinct commits")
    if current_head(root) != train:
        raise InvariantError("HEAD must equal old train head before neutralization")
    assert_clean_worktree(root)

    merge_base, _base_tree, train_tree, main_tree = _require_allowlisted_train_delta(
        root, train, main, generated, require_delta=True
    )

    for path in generated:
        literal = f":(literal){path}"
        if path in main_tree:
            _git(
                root,
                "restore",
                f"--source={main}",
                "--staged",
                "--worktree",
                "--",
                literal,
            )
        elif path in train_tree:
            _git(root, "rm", "-f", "--", literal)

    _assert_worktree_matches_index(root)
    staged_tree_oid = _git(root, "write-tree").stdout.strip()
    staged_tree = tree_entries(root, staged_tree_oid)
    expected_tree = _tree_with_main_generated(train_tree, main_tree, generated)
    if staged_tree != expected_tree:
        extras = sorted(changed_paths(expected_tree, staged_tree))
        raise InvariantError(
            "neutralized index differs from its exact expected tree: "
            + ", ".join(extras)
        )

    staged_changes = sorted(changed_paths(train_tree, staged_tree))
    extra_staged = sorted(set(staged_changes) - set(generated))
    if extra_staged:
        raise InvariantError(
            "neutralization staged non-generated paths: " + ", ".join(extra_staged)
        )
    for path in generated:
        if staged_tree.get(path) != main_tree.get(path):
            raise InvariantError(
                f"generated path does not exactly match trusted main after neutralization: {path}"
            )

    return {
        "changed": bool(staged_changes),
        "changed_paths": staged_changes,
        "generated_paths": list(generated),
        "merge_base": merge_base,
        "old_train_head": train,
        "trusted_main": main,
    }


def _commit_parents(repo: Path, commit: str) -> list[str]:
    line = _git(repo, "rev-list", "--parents", "-n", "1", commit).stdout.strip()
    fields = line.split()
    if not fields or fields[0] != commit:
        raise InvariantError(f"could not read exact parent list for merge commit {commit}")
    return fields[1:]


def validate_merge(
    repo: str | os.PathLike[str],
    *,
    merge_commit: str,
    old_train_head: str,
    trusted_main: str,
    generated_paths: Iterable[str],
) -> dict[str, object]:
    root = repository_root(repo)
    generated = validate_generated_paths(generated_paths)
    merge = validate_commit(root, merge_commit, "merge commit")
    train = validate_commit(root, old_train_head, "old train head")
    main = validate_commit(root, trusted_main, "trusted main")
    if len({merge, train, main}) != 3:
        raise InvariantError("merge, train, and trusted-main commits must be distinct")
    if current_head(root) != merge:
        raise InvariantError("HEAD must equal the returned merge commit during validation")
    assert_clean_worktree(root)

    parents = _commit_parents(root, merge)
    if parents != [train, main]:
        rendered = ", ".join(parents) if parents else "<none>"
        raise InvariantError(
            "merge commit must have exactly the ordered parents "
            f"[old train head, trusted main]; found [{rendered}]"
        )

    _merge_base, _base_tree, train_tree, main_tree = _require_allowlisted_train_delta(
        root, train, main, generated, require_delta=False
    )
    merge_tree = tree_entries(root, merge)
    _assert_safe_tree(merge_tree, "merge tree")

    generated_set = set(generated)
    train_generated_drift = sorted(
        path
        for path in generated
        if train_tree.get(path) != main_tree.get(path)
    )
    if train_generated_drift:
        raise InvariantError(
            "old train head was not neutralized to trusted-main generated bytes/modes: "
            + ", ".join(train_generated_drift)
        )

    drift = changed_paths(main_tree, merge_tree)
    generated_drift = sorted(drift & generated_set)
    non_generated_drift = sorted(drift - generated_set)
    if generated_drift:
        raise InvariantError(
            "merge tree regenerated or changed generated paths before validation: "
            + ", ".join(generated_drift)
        )
    if non_generated_drift:
        raise InvariantError(
            "merge tree differs from trusted main outside generated paths: "
            + ", ".join(non_generated_drift)
        )

    return {
        "generated_paths": list(generated),
        "merge_commit": merge,
        "old_train_head": train,
        "parents": parents,
        "trusted_main": main,
        "validated": True,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    neutralize_parser = commands.add_parser(
        "neutralize",
        help="stage generated paths at exact trusted-main bytes and modes",
    )
    neutralize_parser.add_argument("--repo", default=".")
    neutralize_parser.add_argument("--trusted-main", required=True)
    neutralize_parser.add_argument("--old-train-head", required=True)
    neutralize_parser.add_argument(
        "--generated-path",
        action="append",
        required=True,
        dest="generated_paths",
    )

    validate_parser = commands.add_parser(
        "validate-merge",
        help="validate the exact merge commit returned by GitHub",
    )
    validate_parser.add_argument("--repo", default=".")
    validate_parser.add_argument("--merge-commit", required=True)
    validate_parser.add_argument("--trusted-main", required=True)
    validate_parser.add_argument("--old-train-head", required=True)
    validate_parser.add_argument(
        "--generated-path",
        action="append",
        required=True,
        dest="generated_paths",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "neutralize":
            result = neutralize(
                args.repo,
                trusted_main=args.trusted_main,
                old_train_head=args.old_train_head,
                generated_paths=args.generated_paths,
            )
        else:
            result = validate_merge(
                args.repo,
                merge_commit=args.merge_commit,
                trusted_main=args.trusted_main,
                old_train_head=args.old_train_head,
                generated_paths=args.generated_paths,
            )
    except InvariantError as exc:
        print(f"release-branch invariant failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
