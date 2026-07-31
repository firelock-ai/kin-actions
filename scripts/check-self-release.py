#!/usr/bin/env python3
"""Fail-closed version authority for kin-actions' automatic self release."""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path


ALLOWED_PATHS = ("CONTRIBUTING.md", "README.md", "VERSION")
TRAIN_BRANCH = "automation/self-release-next"
FULL_OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


class SelfReleaseGateError(RuntimeError):
    """Self-release authority could not be established."""


def _load_sibling(module_name: str, filename: str):
    """Import a sibling helper from its exact path beside this script."""
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


prepare = _load_sibling("self_release_prepare", "prepare-self-release.py")
train_policy = _load_sibling("release_train_policy", "release_train_policy.py")


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SelfReleaseGateError(
            f"{' '.join(args)} failed: {detail or result.returncode}"
        )
    return result


def _git_show(ref: str, path: str) -> str | None:
    result = _run(["git", "show", f"{ref}:{path}"], check=False)
    return result.stdout if result.returncode == 0 else None


def _commit_available(ref: str) -> bool:
    return (
        _run(["git", "cat-file", "-e", f"{ref}^{{commit}}"], check=False).returncode
        == 0
    )


def read_current_version() -> str:
    path = Path("VERSION")
    if path.is_symlink() or not path.is_file():
        raise SelfReleaseGateError("VERSION is not a regular file")
    return path.read_text(encoding="utf-8").strip()


def is_queue_validation(
    *,
    event_name: str,
    ref_type: str,
    ref_name: str,
    default_branch: str,
) -> bool:
    """Classify a merge-queue speculative validation run.

    The queue re-runs required contexts on a branch it owns. That event carries
    no pull request and no push before object ID, and its range spans every
    pull request in the group, so it proves queued content rather than release
    authority: the pull-request run judges the exact change and the
    default-branch push judges what actually lands. A `merge_group` event on
    anything but that exact ref is not a queue run and fails the gate.
    """

    if event_name != "merge_group":
        return False
    if not train_policy.queue_validation_ref(ref_type, ref_name, default_branch):
        raise SelfReleaseGateError(
            "merge_group must run on the exact "
            f"{default_branch or '<unset>'} merge-queue ref, got "
            f"{ref_type or '<unset>'}:{ref_name or '<unset>'}"
        )
    return True


def select_base_ref(explicit: str, push_before: str, ref_type: str) -> str:
    if explicit:
        return explicit
    if ref_type != "branch":
        raise SelfReleaseGateError(
            "self-release authority requires a branch base"
        )
    if not FULL_OID_RE.fullmatch(push_before or ""):
        raise SelfReleaseGateError(
            f"self-release push requires an exact before object ID, got {push_before!r}"
        )
    if set(push_before) == {"0"}:
        raise SelfReleaseGateError(
            "self-release authority requires an established base commit"
        )
    if not _commit_available(push_before):
        _run(
            ["git", "fetch", "--no-tags", "--depth=1", "origin", push_before],
            check=False,
        )
        if not _commit_available(push_before):
            raise SelfReleaseGateError(
                f"push before commit is unavailable: {push_before}"
            )
    return push_before


def changed_files(base_ref: str) -> list[str]:
    result = _run(
        ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
        check=False,
    )
    if result.returncode != 0:
        result = _run(
            ["git", "diff", "--name-only", base_ref, "HEAD"],
            check=False,
        )
    if result.returncode != 0:
        raise SelfReleaseGateError(
            f"could not resolve exact self-release diff from {base_ref}"
        )
    return sorted(
        line.strip() for line in result.stdout.splitlines() if line.strip()
    )


def remote_tag_commit(version: str) -> str | None:
    tag = f"v{version}"
    result = _run(
        [
            "git",
            "ls-remote",
            "origin",
            f"refs/tags/{tag}",
            f"refs/tags/{tag}^{{}}",
        ],
        check=False,
    )
    if result.returncode != 0:
        raise SelfReleaseGateError(
            f"could not read immutable remote tag state for {tag}"
        )
    rows: dict[str, str] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or not FULL_OID_RE.fullmatch(fields[0]):
            raise SelfReleaseGateError(
                f"malformed remote tag response for {tag}"
            )
        rows[fields[1]] = fields[0]
    unexpected = sorted(
        ref
        for ref in rows
        if ref not in {f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"}
    )
    if unexpected:
        raise SelfReleaseGateError(
            f"remote returned unexpected refs for {tag}: {unexpected}"
        )
    return rows.get(f"refs/tags/{tag}^{{}}") or rows.get(f"refs/tags/{tag}")


def validate_document_pins(root: Path, version: str) -> list[str]:
    failures: list[str] = []
    for relative in ("README.md", "CONTRIBUTING.md"):
        path = root / relative
        if path.is_symlink() or not path.is_file():
            failures.append(f"{relative} is not a regular generated document")
            continue
        matches = list(prepare.PIN_RE.finditer(path.read_text(encoding="utf-8")))
        if not matches:
            failures.append(f"{relative} has no exact kin-actions version pin")
            continue
        mismatches = sorted(
            {match.group("version") for match in matches}
            - {version}
        )
        if mismatches:
            failures.append(
                f"{relative} has pins not equal to VERSION {version}: "
                + ", ".join(mismatches)
            )
    return failures


def evaluate(
    *,
    current_version: str,
    base_version: str,
    changed_paths: list[str],
    event_name: str,
    ref_type: str,
    ref_name: str,
    default_branch: str,
    base_repo: str,
    head_repo: str,
    head_branch: str,
    labels: list[str],
    remote_tag_sha: str | None,
    head_sha: str,
    pin_failures: list[str],
) -> dict[str, object]:
    failures = list(pin_failures)
    current = prepare.parse_version(current_version)
    base = prepare.parse_version(base_version)
    changed = set(changed_paths)
    moved = current != base
    intent = ""

    if moved:
        successors = {
            level: prepare.successor(base, level)
            for level in ("patch", "minor", "major")
        }
        matches = [
            level for level, target in successors.items() if current == target
        ]
        if len(matches) != 1:
            failures.append(
                f"VERSION must be one exact automatic successor of "
                f"{base_version}, got {current_version}"
            )
        else:
            intent = matches[0]

        extras = sorted(changed - set(ALLOWED_PATHS))
        if extras:
            failures.append(
                "version-moving change contains non-generated paths: "
                + ", ".join(extras)
            )
        if "VERSION" not in changed:
            failures.append("version moved without VERSION in the exact diff")

        normalized_labels = {label.strip().lower() for label in labels}
        if event_name == "pull_request":
            trusted = (
                bool(base_repo)
                and head_repo == base_repo
                and head_branch == TRAIN_BRANCH
                and "release:automated" in normalized_labels
            )
            if not trusted:
                failures.append(
                    "only the exact first-party automatic self-release PR "
                    "may move VERSION"
                )
            if remote_tag_sha is not None:
                failures.append(
                    f"v{current_version} already exists; immutable release "
                    "authority is consumed"
                )
        elif event_name == "push":
            if (
                ref_type != "branch"
                or not default_branch
                or ref_name != default_branch
            ):
                failures.append(
                    "version-moving push is not the exact default branch"
                )
            if remote_tag_sha is not None and remote_tag_sha != head_sha:
                failures.append(
                    f"v{current_version} exists at {remote_tag_sha}, "
                    f"not exact head {head_sha}"
                )
        else:
            failures.append(
                f"event {event_name!r} cannot move self-release VERSION"
            )

    return {
        "failures": failures,
        "release_candidate": moved and not failures,
        "intent": intent,
    }


def _write_outputs(result: dict[str, object]) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return
    with open(output, "a", encoding="utf-8") as stream:
        stream.write(
            "release_candidate="
            + ("true" if result["release_candidate"] else "false")
            + "\n"
        )
        stream.write(f"intent={result['intent']}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref", default="")
    parser.add_argument("--push-before", default="")
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--ref-type", default="")
    parser.add_argument("--ref-name", default="")
    parser.add_argument("--default-branch", required=True)
    parser.add_argument("--base-repo", required=True)
    parser.add_argument("--head-repo", default="")
    parser.add_argument("--head-branch", default="")
    parser.add_argument("--labels", default="")
    args = parser.parse_args(argv)

    try:
        current_version = read_current_version()
        if is_queue_validation(
            event_name=args.event_name,
            ref_type=args.ref_type,
            ref_name=args.ref_name,
            default_branch=args.default_branch,
        ):
            prepare.parse_version(current_version)
            _write_outputs({"release_candidate": False, "intent": ""})
            print("Kin actions self-release gate")
            print("  context           : merge-queue validation")
            print(f"  queue ref         : {args.ref_name}")
            print(f"  current version   : {current_version}")
            print("  release candidate : False")
            return 0
        base_ref = select_base_ref(
            args.base_ref, args.push_before, args.ref_type
        )
        base_text = _git_show(base_ref, "VERSION")
        if base_text is None:
            raise SelfReleaseGateError(
                f"VERSION is unavailable at exact base {base_ref}"
            )
        base_version = base_text.strip()
        changed = changed_files(base_ref)
        labels = [
            item
            for item in re.split(r"[\s,]+", args.labels.strip())
            if item
        ]
        head_sha = _run(["git", "rev-parse", "HEAD"]).stdout.strip()
        if not FULL_OID_RE.fullmatch(head_sha):
            raise SelfReleaseGateError("HEAD is not an exact object ID")
        moved = (
            prepare.parse_version(current_version)
            != prepare.parse_version(base_version)
        )
        tag_sha = remote_tag_commit(current_version) if moved else None
        pin_failures = (
            validate_document_pins(Path.cwd(), current_version)
            if moved
            else []
        )
        result = evaluate(
            current_version=current_version,
            base_version=base_version,
            changed_paths=changed,
            event_name=args.event_name,
            ref_type=args.ref_type,
            ref_name=args.ref_name,
            default_branch=args.default_branch,
            base_repo=args.base_repo,
            head_repo=args.head_repo,
            head_branch=args.head_branch,
            labels=labels,
            remote_tag_sha=tag_sha,
            head_sha=head_sha,
            pin_failures=pin_failures,
        )
        if moved and not result["failures"]:
            prepare.verify_self_release(
                root=Path.cwd(),
                base_ref=base_ref,
                base_version=base_version,
                target_version=current_version,
                generated_paths=list(ALLOWED_PATHS),
            )
    except (OSError, SelfReleaseGateError, prepare.SelfReleaseError) as exc:
        print(f"self-release gate failed: {exc}", file=sys.stderr)
        return 1

    _write_outputs(result)
    print("Kin actions self-release gate")
    print(f"  base version      : {base_version}")
    print(f"  current version   : {current_version}")
    print(f"  release candidate : {result['release_candidate']}")
    print(f"  intent            : {result['intent'] or '<none>'}")
    if result["failures"]:
        for failure in result["failures"]:
            print(f"  - {failure}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
