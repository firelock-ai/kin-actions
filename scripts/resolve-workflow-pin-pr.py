#!/usr/bin/env python3
"""Resolve the open workflow-pin PR and prove it carries the exact pushed head.

A pull request object trails the ref it tracks. Reading ``headRefOid`` in the
same second as the push that moved the branch therefore returns the pre-push
commit often enough that the pin wave rejected its own work, one repository at
a time, while the next run over an unchanged branch passed. The read is retried
until the API reports the commit this run actually pushed.

Retrying does not soften the guarantee. The pull request still has to name the
exact generated commit and live in the target repository's own namespace, so a
head that genuinely points somewhere else never converges and is refused, and a
head belonging to another owner is refused on sight rather than waited on.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time


OBJECT_ID_LENGTHS = {40, 64}


class ResolveError(RuntimeError):
    """A workflow-pin PR invariant failed."""


def validate_object_id(value: str, label: str) -> str:
    candidate = value.strip()
    if len(candidate) not in OBJECT_ID_LENGTHS or any(
        character not in "0123456789abcdef" for character in candidate
    ):
        raise ResolveError(f"{label} is not a lowercase hex object id: {value!r}")
    return candidate


def validate_repository(value: str) -> tuple[str, str]:
    owner, separator, name = value.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise ResolveError(f"repository must be OWNER/NAME, got {value!r}")
    return owner, name


def list_open_prs(*, repository: str, base: str, head_branch: str) -> list[dict]:
    """Return the open PRs GitHub reports for one head branch."""

    result = subprocess.run(
        [
            "gh", "pr", "list",
            "--repo", repository,
            "--state", "open",
            "--base", base,
            "--head", head_branch,
            "--json", "number,headRefOid,headRepositoryOwner",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ResolveError(
            "could not list workflow-pin pull requests"
            + (f": {detail}" if detail else "")
        )
    try:
        entries = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ResolveError(f"pull request listing is not JSON: {exc}") from exc
    if not isinstance(entries, list):
        raise ResolveError("pull request listing is not a JSON array")
    for entry in entries:
        if not isinstance(entry, dict):
            raise ResolveError("pull request listing carries a non-object entry")
    return entries


def resolve_pin_pr(
    *,
    repository: str,
    base: str,
    head_branch: str,
    expect_head: str,
    attempts: int,
    delay: float,
    sleep=time.sleep,
) -> int:
    """Return the PR number whose head is exactly ``expect_head``."""

    owner, _name = validate_repository(repository)
    expected = validate_object_id(expect_head, "generated head")
    if attempts < 1:
        raise ResolveError("attempts must be at least 1")

    last_reason = f"no open workflow-pin PR claims {head_branch}"
    for attempt in range(attempts):
        if attempt:
            sleep(delay)
        entries = list_open_prs(
            repository=repository, base=base, head_branch=head_branch
        )
        # Ambiguity and foreign ownership are decisions, not lag. Waiting on
        # either would turn a refusal into a timeout and hide why it happened.
        if len(entries) > 1:
            raise ResolveError(f"multiple workflow-pin PRs claim {head_branch}")
        if not entries:
            continue
        entry = entries[0]
        number = entry.get("number")
        if not isinstance(number, int):
            raise ResolveError("pull request listing carries no PR number")
        pr_owner = ""
        head_owner = entry.get("headRepositoryOwner")
        if isinstance(head_owner, dict):
            pr_owner = head_owner.get("login") or ""
        if pr_owner != owner:
            raise ResolveError(
                f"workflow-pin PR #{number} is not the exact first-party "
                f"generated head: head repository owner is {pr_owner!r}, "
                f"not {owner!r}"
            )
        head = entry.get("headRefOid") or ""
        if head == expected:
            return number
        last_reason = (
            f"workflow-pin PR #{number} is not the exact first-party generated "
            f"head: PR reports {head or '<unknown>'}, this run pushed {expected}"
        )
    raise ResolveError(last_reason)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head-branch", required=True)
    parser.add_argument("--expect-head", required=True)
    parser.add_argument("--attempts", type=int, default=10)
    parser.add_argument("--delay", type=float, default=3.0)
    args = parser.parse_args(argv)
    try:
        number = resolve_pin_pr(
            repository=args.repository,
            base=args.base,
            head_branch=args.head_branch,
            expect_head=args.expect_head,
            attempts=args.attempts,
            delay=args.delay,
        )
    except ResolveError as exc:
        print(f"workflow-pin PR resolution failed: {exc}", file=sys.stderr)
        return 1
    print(number)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
