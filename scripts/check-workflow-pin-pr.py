#!/usr/bin/env python3
"""Prove an exact workflow-pin PR head before auto-merge is armed."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path


SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
GOOD_CONCLUSIONS = {"success", "skipped"}
GOOD_REQUIRED_BUCKETS = {"pass", "skipping"}
WAITING_REQUIRED_BUCKETS = {"pending"}


def _load_protection():
    path = Path(__file__).with_name("workflow-pin-protection.py")
    spec = importlib.util.spec_from_file_location("workflow_pin_protection_for_pr", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


protection_guard = _load_protection()
CARGO_CONTEXTS = protection_guard.CARGO_CONTEXTS


class AdmissionError(RuntimeError):
    """The PR or check evidence cannot authorize auto-merge."""


def evaluate_admission(
    *,
    pr: dict,
    check_runs: list[dict],
    required_checks: list[dict],
    repository_settings: dict,
    status_checks: dict,
    repository: str,
    base: str,
    base_sha: str,
    head_branch: str,
    head_sha: str,
    kind: str,
    required_app_id: int,
) -> dict:
    activation = protection_guard.validate_protection(
        repository_settings=repository_settings,
        status_checks=status_checks,
        repository=repository,
        kind=kind,
        required_app_id=required_app_id,
    )
    if activation["default_branch"] != base:
        raise AdmissionError("workflow-pin PR base is not the live protected default branch")
    expected_apps = activation["required_checks"]
    if pr.get("state") != "open" or pr.get("draft") is True:
        raise AdmissionError("workflow-pin PR must be open and non-draft")
    head = pr.get("head")
    base_data = pr.get("base")
    if not isinstance(head, dict) or not isinstance(base_data, dict):
        raise AdmissionError("workflow-pin PR lacks exact head/base identity")
    head_repo = head.get("repo")
    if not isinstance(head_repo, dict) or head_repo.get("full_name") != repository:
        raise AdmissionError("workflow-pin PR head must be first-party")
    if head.get("ref") != head_branch or head.get("sha") != head_sha:
        raise AdmissionError("workflow-pin PR head differs from the generated head")
    if base_data.get("ref") != base or base_data.get("sha") != base_sha:
        raise AdmissionError("workflow-pin PR base differs from exact trusted main")

    if not check_runs:
        return {"status": "waiting", "reason": "no check runs report on the exact head"}
    names: dict[str, dict] = {}
    for check in check_runs:
        name = check.get("name")
        if not isinstance(name, str) or not name:
            raise AdmissionError("exact-head check run has no stable name")
        if name in names:
            raise AdmissionError(f"duplicate latest exact-head check run: {name}")
        names[name] = check
        if check.get("status") != "completed":
            return {"status": "waiting", "reason": f"check is not complete: {name}"}
        conclusion = check.get("conclusion")
        if conclusion not in GOOD_CONCLUSIONS:
            raise AdmissionError(
                f"exact-head check is not green: {name} ({conclusion})"
            )

    if not required_checks and kind == "cargo_release":
        return {
            "status": "waiting",
            "reason": "no required Cargo exact-head check is registered yet",
        }
    required_names: set[str] = set()
    required_buckets: dict[str, str] = {}
    for check in required_checks:
        name = check.get("name")
        bucket = check.get("bucket")
        if not isinstance(name, str) or not name:
            raise AdmissionError("required check has no stable name")
        if name in required_names:
            raise AdmissionError(f"duplicate required check: {name}")
        required_names.add(name)
        required_buckets[name] = bucket
        if bucket in WAITING_REQUIRED_BUCKETS:
            return {"status": "waiting", "reason": f"required check is pending: {name}"}
        if bucket not in GOOD_REQUIRED_BUCKETS:
            raise AdmissionError(f"required check is not green: {name} ({bucket})")
        if name not in names:
            raise AdmissionError(f"required check is absent from the full check-run set: {name}")
    if required_names != set(expected_apps):
        raise AdmissionError(
            "live required-check query differs from strict protection: "
            + ", ".join(sorted(required_names ^ set(expected_apps)))
        )
    for name, app_id in expected_apps.items():
        app = names[name].get("app")
        if not isinstance(app, dict) or app.get("id") != app_id:
            raise AdmissionError(f"required check has wrong app authority: {name}")

    if kind == "cargo_release":
        missing = sorted(CARGO_CONTEXTS - set(names))
        if missing:
            raise AdmissionError(
                "cargo caller lacks protected release checks: " + ", ".join(missing)
            )
        for name in CARGO_CONTEXTS:
            if names[name].get("conclusion") != "success" or required_buckets.get(name) != "pass":
                raise AdmissionError(
                    f"protected Cargo release check did not pass: {name}"
                )
    elif kind != "other":
        raise AdmissionError(f"unknown consumer kind: {kind!r}")

    return {
        "status": "ready",
        "head_sha": head_sha,
        "check_runs": len(check_runs),
        "required_checks": len(required_checks),
    }


def _json_command(command: list[str], *, allow_nonzero: bool = False):
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0 and not allow_nonzero:
        raise AdmissionError(
            f"command failed ({result.returncode}): {' '.join(command)}: "
            f"{result.stderr.strip()}"
        )
    try:
        return json.loads(result.stdout or "[]"), result.returncode
    except json.JSONDecodeError as exc:
        raise AdmissionError(
            f"command returned invalid JSON: {' '.join(command)}"
        ) from exc


def fetch_evidence(repository: str, pr_number: int, head_sha: str):
    repository_settings, status_checks = protection_guard.fetch_live(repository)
    pr, _ = _json_command(
        ["gh", "api", f"repos/{repository}/pulls/{pr_number}"]
    )
    pages, _ = _json_command(
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            f"repos/{repository}/commits/{head_sha}/check-runs?filter=latest&per_page=100",
        ]
    )
    check_runs: list[dict] = []
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("check_runs"), list):
            raise AdmissionError("check-run pagination returned an invalid page")
        check_runs.extend(page["check_runs"])
    required, code = _json_command(
        [
            "gh",
            "pr",
            "checks",
            str(pr_number),
            "--repo",
            repository,
            "--required",
            "--json",
            "name,bucket,state,workflow",
        ],
        allow_nonzero=True,
    )
    if code not in {0, 1, 8}:
        raise AdmissionError(f"gh pr checks returned unexpected status {code}")
    if not isinstance(required, list):
        raise AdmissionError("required-check query returned invalid JSON")
    return pr, check_runs, required, repository_settings, status_checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--head-branch", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--kind", choices=("cargo_release", "other"), required=True)
    parser.add_argument("--required-app-id", type=int, required=True)
    args = parser.parse_args(argv)
    if not os.environ.get("GH_TOKEN"):
        print("workflow-pin PR admission failed: GH_TOKEN is required", file=sys.stderr)
        return 1
    if (
        not REPOSITORY_RE.fullmatch(args.repository)
        or not SHA_RE.fullmatch(args.head_sha)
        or not SHA_RE.fullmatch(args.base_sha)
    ):
        print("workflow-pin PR admission failed: unsafe repository or SHA", file=sys.stderr)
        return 1
    try:
        pr, checks, required, settings, status_checks = fetch_evidence(
            args.repository, args.pr, args.head_sha
        )
        result = evaluate_admission(
            pr=pr,
            check_runs=checks,
            required_checks=required,
            repository_settings=settings,
            status_checks=status_checks,
            repository=args.repository,
            base=args.base,
            base_sha=args.base_sha,
            head_branch=args.head_branch,
            head_sha=args.head_sha,
            kind=args.kind,
            required_app_id=args.required_app_id,
        )
    except (AdmissionError, protection_guard.ProtectionError) as exc:
        print(f"workflow-pin PR admission failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
