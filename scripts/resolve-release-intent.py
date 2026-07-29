#!/usr/bin/env python3
"""Resolve automatic release intent from immutable first-parent commits.

The optional ``Kin-Release-Intent`` commit trailer is the only escalation
authority.  Mutable pull-request labels are deliberately ignored.  Source
drift defaults to ``patch``; a valid ``minor`` or ``major`` trailer on any
landed first-parent commit escalates the coalesced train.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


TRAILER_KEY = "Kin-Release-Intent"
INTENTS = ("patch", "minor", "major")
RANK = {intent: index for index, intent in enumerate(INTENTS)}
RAW_TRAILER_RE = re.compile(
    rf"(?im)^[ \t]*{re.escape(TRAILER_KEY)}\b[^\r\n]*$"
)
PARSED_TRAILER_RE = re.compile(
    rf"(?i)^{re.escape(TRAILER_KEY)}:\s*(\S+)\s*$"
)


class ReleaseIntentError(RuntimeError):
    """Immutable release intent could not be resolved safely."""


def _run(
    args: list[str],
    *,
    root: Path,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        args,
        cwd=root,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ReleaseIntentError(
            f"{' '.join(args)} failed: {detail or result.returncode}"
        )
    return result


def _commit_intent(root: Path, commit: str) -> str | None:
    message = _run(
        ["git", "--no-replace-objects", "show", "-s", "--format=%B", commit],
        root=root,
    ).stdout
    raw_mentions = RAW_TRAILER_RE.findall(message)
    parsed = _run(
        ["git", "interpret-trailers", "--parse"],
        root=root,
        input_text=message,
    ).stdout.splitlines()
    parsed_intents = []
    for line in parsed:
        match = PARSED_TRAILER_RE.fullmatch(line.strip())
        if match:
            parsed_intents.append(match.group(1).lower())

    if raw_mentions and len(parsed_intents) != len(raw_mentions):
        raise ReleaseIntentError(
            f"{commit} has malformed or non-footer {TRAILER_KEY} evidence"
        )
    if len(parsed_intents) > 1:
        raise ReleaseIntentError(
            f"{commit} has duplicate {TRAILER_KEY} trailers"
        )
    if not parsed_intents:
        return None
    intent = parsed_intents[0]
    if intent not in RANK:
        raise ReleaseIntentError(
            f"{commit} has invalid {TRAILER_KEY}: {intent!r}"
        )
    return intent


def resolve_release_intent(
    *,
    root: Path,
    base_ref: str,
    head_ref: str = "HEAD",
) -> dict[str, object]:
    root = root.resolve()
    ancestor = _run(
        [
            "git",
            "--no-replace-objects",
            "merge-base",
            "--is-ancestor",
            base_ref,
            head_ref,
        ],
        root=root,
        check=False,
    )
    if ancestor.returncode != 0:
        raise ReleaseIntentError(
            f"{base_ref} is not an ancestor of {head_ref}"
        )
    commits = _run(
        [
            "git",
            "--no-replace-objects",
            "rev-list",
            "--first-parent",
            "--reverse",
            f"{base_ref}..{head_ref}",
        ],
        root=root,
    ).stdout.splitlines()

    evidence: list[dict[str, str]] = []
    intent = "patch"
    for commit in commits:
        found = _commit_intent(root, commit)
        if found is not None:
            evidence.append({"commit": commit, "intent": found})
            if RANK[found] > RANK[intent]:
                intent = found
    return {
        "base_ref": base_ref,
        "head_ref": head_ref,
        "intent": intent,
        "evidence": evidence,
    }


def _write_outputs(result: dict[str, object]) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return
    with open(output, "a", encoding="utf-8") as stream:
        stream.write(f"intent={result['intent']}\n")
        stream.write(
            "evidence_json="
            + json.dumps(result["evidence"], separators=(",", ":"))
            + "\n"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--head-ref", default="HEAD")
    args = parser.parse_args(argv)
    try:
        result = resolve_release_intent(
            root=args.root,
            base_ref=args.base_ref,
            head_ref=args.head_ref,
        )
    except (OSError, ReleaseIntentError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _write_outputs(result)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
