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


def required_checks(value: dict[str, object]) -> dict[str, list[int | None]]:
    """Return each required check and the GitHub App IDs allowed to satisfy it."""

    raw_contexts = value.get("contexts", [])
    if not isinstance(raw_contexts, list):
        raise ActivationError("required status contexts must be a JSON list")
    for context in raw_contexts:
        if not isinstance(context, str) or not context:
            raise ActivationError("required status contexts must be non-empty strings")

    checks: dict[str, list[int | None]] = {}
    raw_checks = value.get("checks", [])
    if not isinstance(raw_checks, list):
        raise ActivationError("required status checks must be a JSON list")
    for check in raw_checks:
        if not isinstance(check, dict):
            raise ActivationError("required status check entries must be objects")
        context = check.get("context")
        if not isinstance(context, str) or not context:
            raise ActivationError("required status check context is missing")
        app_id = check.get("app_id")
        if app_id is not None and (
            not isinstance(app_id, int) or isinstance(app_id, bool) or app_id <= 0
        ):
            raise ActivationError(
                f"required status check {context!r} has invalid app_id {app_id!r}"
            )
        checks.setdefault(context, []).append(app_id)
    return checks


def validate(
    repository: dict[str, object],
    protection: dict[str, object] | None,
    required: Sequence[str],
    required_check_app_id: int | None = None,
) -> None:
    """Validate squash-only intent preservation and required-check admission."""

    expected_settings: tuple[tuple[str, object], ...] = (
        ("allow_auto_merge", True),
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
    if (
        not isinstance(required_check_app_id, int)
        or isinstance(required_check_app_id, bool)
        or required_check_app_id <= 0
    ):
        raise ActivationError(
            "a positive required-check App ID is required for automatic release"
        )
    if protection is None:
        raise ActivationError("required status-check protection JSON is missing")
    if protection.get("strict") is not True:
        raise ActivationError(
            "required status checks must enforce strict/up-to-date branches"
        )
    actual = required_checks(protection)
    missing = [
        context
        for context in requested
        if required_check_app_id not in actual.get(context, set())
    ]
    if missing:
        raise ActivationError(
            "main does not require App-bound automatic-release checks: "
            + ", ".join(missing)
        )
    ambiguous = [
        context
        for context in requested
        if actual.get(context) != [required_check_app_id]
    ]
    if ambiguous:
        raise ActivationError(
            "automatic-release checks permit an unbound or wrong App writer: "
            + ", ".join(ambiguous)
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-settings", type=Path, required=True)
    parser.add_argument("--status-checks", type=Path)
    parser.add_argument("--required-context", action="append", default=[])
    parser.add_argument("--required-check-app-id", type=int)
    args = parser.parse_args(argv)

    try:
        repository = _read_object(args.repository_settings)
        protection = _read_object(args.status_checks) if args.status_checks else None
        validate(
            repository,
            protection,
            args.required_context,
            args.required_check_app_id,
        )
    except ActivationError as exc:
        print(f"release activation refused: {exc}", file=sys.stderr)
        return 1
    print("release activation settings verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
