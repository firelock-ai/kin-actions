#!/usr/bin/env python3
"""Fail closed unless repository settings can admit unattended releases safely."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


class ActivationError(RuntimeError):
    """The repository is not configured for unattended release admission."""


def _read_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ActivationError(f"cannot read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ActivationError(f"{path} must contain one JSON object")
    return value


def required_contexts(value: dict[str, object]) -> set[str]:
    """Return every exact context named by GitHub's protection response."""

    contexts: set[str] = set()
    raw_contexts = value.get("contexts", [])
    if not isinstance(raw_contexts, list):
        raise ActivationError("required status contexts must be a JSON list")
    for context in raw_contexts:
        if not isinstance(context, str) or not context:
            raise ActivationError("required status contexts must be non-empty strings")
        contexts.add(context)

    raw_checks = value.get("checks", [])
    if not isinstance(raw_checks, list):
        raise ActivationError("required status checks must be a JSON list")
    for check in raw_checks:
        if not isinstance(check, dict):
            raise ActivationError("required status check entries must be objects")
        context = check.get("context")
        if not isinstance(context, str) or not context:
            raise ActivationError("required status check context is missing")
        contexts.add(context)
    return contexts


def validate(
    repository: dict[str, object],
    protection: dict[str, object] | None,
    required: Sequence[str],
) -> None:
    """Validate squash-only intent preservation and required-check admission."""

    expected_settings: tuple[tuple[str, object], ...] = (
        ("allow_squash_merge", True),
        ("allow_merge_commit", False),
        ("allow_rebase_merge", False),
        ("squash_merge_commit_title", "PR_TITLE"),
        ("squash_merge_commit_message", "PR_BODY"),
    )
    for setting, expected in expected_settings:
        actual = repository.get(setting)
        if actual != expected:
            raise ActivationError(
                f"{setting} must be {expected!r} for immutable "
                f"Kin-Release-Intent preservation; got {actual!r}"
            )

    requested = tuple(dict.fromkeys(required))
    if not requested:
        return
    if protection is None:
        raise ActivationError("required status-check protection JSON is missing")
    actual = required_contexts(protection)
    missing = [context for context in requested if context not in actual]
    if missing:
        raise ActivationError(
            "main does not require automatic-release admission contexts: "
            + ", ".join(missing)
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-settings", type=Path, required=True)
    parser.add_argument("--status-checks", type=Path)
    parser.add_argument("--required-context", action="append", default=[])
    args = parser.parse_args(argv)

    try:
        repository = _read_object(args.repository_settings)
        protection = _read_object(args.status_checks) if args.status_checks else None
        validate(repository, protection, args.required_context)
    except ActivationError as exc:
        print(f"release activation refused: {exc}", file=sys.stderr)
        return 1
    print("release activation settings verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
