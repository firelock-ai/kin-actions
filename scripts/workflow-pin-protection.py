#!/usr/bin/env python3
"""Prove live merge and strict required-check authority for pin consumers."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from urllib.parse import quote


REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
CARGO_CONTEXTS = {
    "release / Version bump gate",
    "release / Registry-only build",
    "release / Repo verification",
}
EXPECTED_SETTINGS = {
    "allow_auto_merge": True,
    "allow_squash_merge": True,
    "allow_merge_commit": False,
    "allow_rebase_merge": False,
    "squash_merge_commit_title": "PR_TITLE",
    "squash_merge_commit_message": "PR_BODY",
}


class ProtectionError(RuntimeError):
    """Live consumer protection cannot authorize workflow-pin writes."""


def validate_protection(
    *,
    repository_settings: dict,
    status_checks: dict,
    repository: str,
    kind: str,
    required_app_id: int,
) -> dict:
    if repository_settings.get("full_name") != repository:
        raise ProtectionError("repository settings belong to a different repository")
    default_branch = repository_settings.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        raise ProtectionError("repository has no exact default branch")
    for setting, expected in EXPECTED_SETTINGS.items():
        if repository_settings.get(setting) != expected:
            raise ProtectionError(
                f"{repository}: {setting} must be {expected!r}"
            )

    if status_checks.get("strict") is not True:
        raise ProtectionError(
            f"{repository}: required status checks must enforce strict/up-to-date branches"
        )
    contexts = status_checks.get("contexts")
    checks = status_checks.get("checks")
    if not isinstance(contexts, list) or not isinstance(checks, list):
        raise ProtectionError(f"{repository}: required status-check JSON is malformed")
    if not contexts or not checks:
        raise ProtectionError(f"{repository}: at least one required status check is mandatory")
    if any(not isinstance(context, str) or not context for context in contexts):
        raise ProtectionError(f"{repository}: required status contexts must be non-empty strings")
    if len(contexts) != len(set(contexts)):
        raise ProtectionError(f"{repository}: duplicate required status context")

    expected_apps: dict[str, int] = {}
    for check in checks:
        if not isinstance(check, dict):
            raise ProtectionError(f"{repository}: required status check must be an object")
        context = check.get("context")
        app_id = check.get("app_id")
        if not isinstance(context, str) or not context:
            raise ProtectionError(f"{repository}: required status check has no context")
        if context in expected_apps:
            raise ProtectionError(f"{repository}: duplicate required status check {context}")
        if not isinstance(app_id, int) or isinstance(app_id, bool) or app_id <= 0:
            raise ProtectionError(
                f"{repository}: required status check {context!r} is not App-bound"
            )
        expected_apps[context] = app_id
    if set(contexts) != set(expected_apps):
        raise ProtectionError(
            f"{repository}: legacy contexts and App-bound checks differ"
        )

    if kind == "cargo_release":
        missing = sorted(CARGO_CONTEXTS - set(expected_apps))
        if missing:
            raise ProtectionError(
                f"{repository}: missing Cargo release checks: " + ", ".join(missing)
            )
        wrong = sorted(
            context
            for context in CARGO_CONTEXTS
            if expected_apps[context] != required_app_id
        )
        if wrong:
            raise ProtectionError(
                f"{repository}: Cargo release checks have the wrong App: "
                + ", ".join(wrong)
            )
    elif kind != "other":
        raise ProtectionError(f"{repository}: unknown consumer kind {kind!r}")

    return {
        "repository": repository,
        "default_branch": default_branch,
        "required_checks": expected_apps,
    }


def _json_command(command: list[str]):
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise ProtectionError(
            f"command failed ({result.returncode}): {' '.join(command)}: "
            f"{result.stderr.strip()}"
        )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProtectionError(
            f"command returned invalid JSON: {' '.join(command)}"
        ) from exc
    if not isinstance(value, dict):
        raise ProtectionError("GitHub protection query returned a non-object")
    return value


def fetch_live(repository: str) -> tuple[dict, dict]:
    settings = _json_command(["gh", "api", f"repos/{repository}"])
    default_branch = settings.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        raise ProtectionError("repository settings have no default branch")
    status_checks = _json_command(
        [
            "gh",
            "api",
            f"repos/{repository}/branches/{quote(default_branch, safe='')}/protection/required_status_checks",
        ]
    )
    return settings, status_checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--kind", choices=("cargo_release", "other"), required=True)
    parser.add_argument("--required-app-id", type=int, required=True)
    args = parser.parse_args(argv)
    if not os.environ.get("GH_TOKEN"):
        print("workflow-pin protection failed: GH_TOKEN is required", file=sys.stderr)
        return 1
    if not REPOSITORY_RE.fullmatch(args.repository):
        print("workflow-pin protection failed: unsafe repository", file=sys.stderr)
        return 1
    try:
        settings, checks = fetch_live(args.repository)
        result = validate_protection(
            repository_settings=settings,
            status_checks=checks,
            repository=args.repository,
            kind=args.kind,
            required_app_id=args.required_app_id,
        )
    except ProtectionError as exc:
        print(f"workflow-pin protection failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
