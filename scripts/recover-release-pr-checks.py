#!/usr/bin/env python3
"""Bounded recovery for exact-head checks on one generated release PR."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence
from urllib.parse import quote


class RecoveryError(RuntimeError):
    """The generated release PR cannot be recovered safely."""


SUCCESS_CONCLUSIONS = {"success", "neutral", "skipped"}
FAILED_JOB_RERUN_CONCLUSIONS = {"failure", "timed_out", "startup_failure"}
FULL_RERUN_CONCLUSIONS = {"cancelled"}
RUN_ID_RE = re.compile(r"/actions/runs/([0-9]+)(?:/|$)")


def gh_json(args: Sequence[str], token: str) -> object:
    environment = os.environ.copy()
    environment["GH_TOKEN"] = token
    completed = subprocess.run(
        ["gh", *args],
        check=False,
        text=True,
        capture_output=True,
        env=environment,
    )
    if completed.returncode != 0:
        raise RecoveryError(
            f"gh {' '.join(args)} failed: {completed.stderr.strip()}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RecoveryError(
            f"gh {' '.join(args)} returned malformed JSON"
        ) from exc


def gh_no_content(args: Sequence[str], token: str) -> None:
    environment = os.environ.copy()
    environment["GH_TOKEN"] = token
    completed = subprocess.run(
        ["gh", *args],
        check=False,
        text=True,
        capture_output=True,
        env=environment,
    )
    if completed.returncode != 0:
        raise RecoveryError(
            f"gh {' '.join(args)} failed: {completed.stderr.strip()}"
        )


def _object(value: object, description: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RecoveryError(f"{description} must be one JSON object")
    return value


def _list(value: object, description: str) -> list[object]:
    if not isinstance(value, list):
        raise RecoveryError(f"{description} must be one JSON list")
    return value


def _required_specs(
    branch: dict[str, object],
    release_checks: Sequence[str],
    actions_app_id: int,
) -> list[tuple[str, int | None]]:
    protection = _object(branch.get("protection"), "branch protection")
    required = _object(
        protection.get("required_status_checks"),
        "required status checks",
    )
    raw_checks = _list(required.get("checks"), "required check entries")
    specs: list[tuple[str, int | None]] = []
    for raw in raw_checks:
        check = _object(raw, "required check entry")
        context = check.get("context")
        app_id = check.get("app_id")
        if not isinstance(context, str) or not context:
            raise RecoveryError("required check context is missing")
        if app_id is not None and (
            not isinstance(app_id, int)
            or isinstance(app_id, bool)
            or app_id <= 0
        ):
            raise RecoveryError(
                f"required check {context!r} has invalid app_id {app_id!r}"
            )
        specs.append((context, app_id))

    for context in release_checks:
        matches = [spec for spec in specs if spec[0] == context]
        if matches != [(context, actions_app_id)]:
            raise RecoveryError(
                f"release check {context!r} is not uniquely bound to "
                f"GitHub Actions App {actions_app_id}"
            )
    return specs


def _check_runs(value: object) -> list[dict[str, object]]:
    pages = _list(value, "check-run pages")
    runs: list[dict[str, object]] = []
    for page_raw in pages:
        page = _object(page_raw, "check-run page")
        for run_raw in _list(page.get("check_runs"), "check runs"):
            runs.append(_object(run_raw, "check run"))
    return runs


def _latest_matching_check(
    runs: Sequence[dict[str, object]],
    context: str,
    app_id: int | None,
) -> dict[str, object] | None:
    candidates = []
    for run in runs:
        app = _object(run.get("app"), f"check run {context!r} App")
        run_app_id = app.get("id")
        if run.get("name") != context:
            continue
        if app_id is not None and run_app_id != app_id:
            continue
        candidates.append(run)
    if not candidates:
        return None
    return max(candidates, key=lambda run: int(run.get("id", 0)))


def _run_id(check: dict[str, object]) -> int:
    details_url = check.get("details_url")
    if not isinstance(details_url, str):
        raise RecoveryError(
            f"failed check {check.get('name')!r} has no Actions details URL"
        )
    match = RUN_ID_RE.search(details_url)
    if match is None:
        raise RecoveryError(
            f"failed check {check.get('name')!r} is not backed by an Actions run"
        )
    return int(match.group(1))


def _emit(name: str, value: str) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as stream:
            stream.write(f"{name}={value}\n")


def recover(
    *,
    repository: str,
    pull_request: int,
    expected_head: str,
    trusted_main: str,
    default_branch: str,
    release_checks: Sequence[str],
    actions_app_id: int,
    actions_token: str,
    timeout_seconds: int,
    poll_seconds: int,
    max_attempts: int,
) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", expected_head):
        raise RecoveryError("expected PR head must be one lowercase 40-hex SHA")
    if not re.fullmatch(r"[0-9a-f]{40}", trusted_main):
        raise RecoveryError("trusted main must be one lowercase 40-hex SHA")
    if timeout_seconds < 0 or poll_seconds < 0 or max_attempts < 1:
        raise RecoveryError("recovery bounds are invalid")
    if not release_checks:
        raise RecoveryError("required release checks are required")

    owner = repository.split("/", 1)[0]
    started = time.monotonic()
    rerun_attempts: set[tuple[int, int]] = set()
    green_seen = False

    while time.monotonic() - started <= timeout_seconds:
        pr = _object(
            gh_json(
                [
                    "pr",
                    "view",
                    str(pull_request),
                    "--repo",
                    repository,
                    "--json",
                    (
                        "autoMergeRequest,baseRefName,headRefOid,"
                        "headRepositoryOwner,mergedAt,state,url"
                    ),
                ],
                actions_token,
            ),
            "pull request",
        )
        if pr.get("headRefOid") != expected_head:
            raise RecoveryError(
                f"release PR head moved from {expected_head} "
                f"to {pr.get('headRefOid')}"
            )
        head_owner = _object(
            pr.get("headRepositoryOwner"),
            "pull request head owner",
        ).get("login")
        if head_owner != owner:
            raise RecoveryError(
                f"release PR is not first-party: owner is {head_owner!r}"
            )
        if pr.get("mergedAt"):
            _emit("check_recovery_state", "merged")
            return "merged"
        if pr.get("state") != "OPEN":
            raise RecoveryError(
                f"release PR is neither open nor merged: {pr.get('state')!r}"
            )
        if pr.get("baseRefName") != default_branch:
            raise RecoveryError(
                f"release PR base is {pr.get('baseRefName')!r}, "
                f"expected {default_branch!r}"
            )
        if pr.get("autoMergeRequest") is None:
            raise RecoveryError("release PR exact head is not armed for auto-merge")

        main = _object(
            gh_json(
                [
                    "api",
                    f"repos/{repository}/commits/{quote(default_branch, safe='')}",
                ],
                actions_token,
            ),
            "default branch commit",
        )
        if main.get("sha") != trusted_main:
            actual_main = main.get("sha")
            print(
                f"trusted main {trusted_main} advanced to {actual_main}; "
                "leaving stale-head checks untouched",
                file=sys.stderr,
            )
            _emit("check_recovery_state", "superseded")
            raise RecoveryError(
                f"trusted main advanced to {actual_main}; stale-head "
                "auto-merge must be disarmed before coalescing"
            )

        branch = _object(
            gh_json(
                [
                    "api",
                    f"repos/{repository}/branches/{quote(default_branch, safe='')}",
                ],
                actions_token,
            ),
            "default branch",
        )
        specs = _required_specs(branch, release_checks, actions_app_id)
        runs = _check_runs(
            gh_json(
                [
                    "api",
                    "--paginate",
                    "--slurp",
                    (
                        f"repos/{repository}/commits/{expected_head}/"
                        "check-runs?per_page=100"
                    ),
                ],
                actions_token,
            )
        )

        pending = False
        failed: list[dict[str, object]] = []
        for context, app_id in specs:
            check = _latest_matching_check(runs, context, app_id)
            if check is None or check.get("status") != "completed":
                pending = True
                continue
            conclusion = check.get("conclusion")
            if conclusion in SUCCESS_CONCLUSIONS:
                if context in release_checks and conclusion != "success":
                    failed.append(check)
                continue
            failed.append(check)

        if not pending and not failed:
            green_seen = True
            _emit("check_recovery_state", "checks-green-awaiting-merge")
            time.sleep(poll_seconds)
            continue
        if pending and not failed:
            time.sleep(poll_seconds)
            continue

        run_ids: dict[int, tuple[int, str]] = {}
        for check in failed:
            app = _object(check.get("app"), "failed check App")
            if app.get("id") != actions_app_id:
                raise RecoveryError(
                    f"failed required check {check.get('name')!r} is not "
                    f"from GitHub Actions App {actions_app_id}"
                )
            run_id = _run_id(check)
            run = _object(
                gh_json(
                    ["api", f"repos/{repository}/actions/runs/{run_id}"],
                    actions_token,
                ),
                f"Actions run {run_id}",
            )
            head_repository = _object(
                run.get("head_repository"),
                f"Actions run {run_id} head repository",
            )
            if (
                run.get("head_sha") != expected_head
                or run.get("event") != "pull_request"
                or head_repository.get("full_name") != repository
            ):
                raise RecoveryError(
                    f"refusing unexpected Actions run {run_id} for "
                    f"failed check {check.get('name')!r}"
                )
            if run.get("status") != "completed":
                continue
            attempt = run.get("run_attempt")
            if not isinstance(attempt, int) or isinstance(attempt, bool):
                raise RecoveryError(
                    f"Actions run {run_id} has invalid attempt {attempt!r}"
                )
            if attempt >= max_attempts:
                raise RecoveryError(
                    f"Actions run {run_id} exhausted bounded attempt "
                    f"{attempt}/{max_attempts}"
                )
            run_conclusion = run.get("conclusion")
            if run_conclusion in FAILED_JOB_RERUN_CONCLUSIONS:
                rerun_endpoint = "rerun-failed-jobs"
            elif run_conclusion in FULL_RERUN_CONCLUSIONS:
                rerun_endpoint = "rerun"
            else:
                raise RecoveryError(
                    f"Actions run {run_id} conclusion "
                    f"{run_conclusion!r} has no safe automatic rerun"
                )
            run_ids[run_id] = (attempt, rerun_endpoint)

        for run_id, (attempt, rerun_endpoint) in run_ids.items():
            key = (run_id, attempt)
            if key in rerun_attempts:
                continue
            print(
                f"requesting {rerun_endpoint} for exact-head Actions run "
                f"{run_id} "
                f"at bounded attempt {attempt + 1}/{max_attempts}",
                file=sys.stderr,
            )
            gh_no_content(
                [
                    "api",
                    "--method",
                    "POST",
                    (
                        f"repos/{repository}/actions/runs/{run_id}/"
                        f"{rerun_endpoint}"
                    ),
                ],
                actions_token,
            )
            rerun_attempts.add(key)
        time.sleep(poll_seconds)

    if green_seen:
        raise RecoveryError(
            "release PR checks became green but exact-head auto-merge did not "
            f"complete within {timeout_seconds}s"
        )
    raise RecoveryError(
        f"release PR {pull_request} checks did not recover within "
        f"{timeout_seconds}s"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pull-request", type=int, required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--trusted-main", required=True)
    parser.add_argument("--default-branch", required=True)
    parser.add_argument("--required-release-check", action="append", default=[])
    parser.add_argument("--actions-app-id", type=int, default=15368)
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(os.environ.get("KIN_PR_CHECK_TIMEOUT_SECONDS", "1800")),
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=int(os.environ.get("KIN_PR_CHECK_POLL_SECONDS", "10")),
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=int(os.environ.get("KIN_PR_CHECK_MAX_ATTEMPTS", "3")),
    )
    args = parser.parse_args(argv)
    token = os.environ.get("KIN_ACTIONS_TOKEN", "")
    if not token:
        print("release PR check recovery refused: KIN_ACTIONS_TOKEN is required", file=sys.stderr)
        return 1
    try:
        state = recover(
            repository=args.repository,
            pull_request=args.pull_request,
            expected_head=args.expected_head,
            trusted_main=args.trusted_main,
            default_branch=args.default_branch,
            release_checks=args.required_release_check,
            actions_app_id=args.actions_app_id,
            actions_token=token,
            timeout_seconds=args.timeout_seconds,
            poll_seconds=args.poll_seconds,
            max_attempts=args.max_attempts,
        )
    except RecoveryError as exc:
        print(f"release PR check recovery refused: {exc}", file=sys.stderr)
        return 1
    print(f"release PR check recovery state: {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
