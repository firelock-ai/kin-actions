#!/usr/bin/env python3
"""Bounded recovery for one exact pre-publication Registry Publish run."""

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


class PublicationRecoveryError(RuntimeError):
    """The exact registry publication cannot be recovered safely."""


ABSENT_STATES = {"unpublished", "version-absent"}
FAILED_JOB_RERUN_CONCLUSIONS = {"failure", "timed_out", "startup_failure"}
FULL_RERUN_CONCLUSIONS = {"cancelled"}


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
        raise PublicationRecoveryError(
            f"gh {' '.join(args)} failed: {completed.stderr.strip()}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise PublicationRecoveryError(
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
        raise PublicationRecoveryError(
            f"gh {' '.join(args)} failed: {completed.stderr.strip()}"
        )


def inspect_registry(
    *,
    helper_root: Path,
    registry_url: str,
    package: str,
    version: str,
) -> dict[str, object]:
    completed = subprocess.run(
        [
            sys.executable,
            str(helper_root / "scripts" / "inspect-registry-version.py"),
            "--registry-url",
            registry_url,
            "--package",
            package,
            "--version",
            version,
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise PublicationRecoveryError(
            "registry inspection failed: " + completed.stderr.strip()
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise PublicationRecoveryError(
            "registry inspection returned malformed JSON"
        ) from exc
    if not isinstance(value, dict):
        raise PublicationRecoveryError(
            "registry inspection must return one JSON object"
        )
    return value


def _run(
    runs: object,
    *,
    workflow: str,
    branch: str,
    commit: str,
) -> dict[str, object] | None:
    if not isinstance(runs, list):
        raise PublicationRecoveryError("workflow runs must be one JSON list")
    matches = []
    for raw in runs:
        if not isinstance(raw, dict):
            raise PublicationRecoveryError("workflow run entry must be an object")
        if (
            raw.get("event") == "push"
            and raw.get("headBranch") == branch
            and raw.get("headSha") == commit
            and raw.get("workflowName") == workflow
        ):
            matches.append(raw)
    if not matches:
        return None
    return max(
        matches,
        key=lambda value: (
            str(value.get("createdAt", "")),
            int(value.get("databaseId", 0)),
        ),
    )


def _emit(name: str, value: str) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as stream:
            stream.write(f"{name}={value}\n")


def recover(
    *,
    repository: str,
    package: str,
    version: str,
    version_commit: str,
    workflow: str,
    default_branch: str,
    registry_url: str,
    helper_root: Path,
    actions_token: str,
    timeout_seconds: int,
    visibility_seconds: int,
    poll_seconds: int,
    max_attempts: int,
) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", version_commit):
        raise PublicationRecoveryError(
            "version commit must be one lowercase 40-hex SHA"
        )
    if not re.fullmatch(
        r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)",
        version,
    ):
        raise PublicationRecoveryError("version must be stable numeric SemVer")
    if (
        timeout_seconds < 0
        or visibility_seconds < 0
        or poll_seconds < 0
        or max_attempts < 1
    ):
        raise PublicationRecoveryError("publication recovery bounds are invalid")

    started = time.monotonic()
    failed_seen: dict[tuple[int, int], float] = {}
    rerun_attempts: set[tuple[int, int]] = set()
    while time.monotonic() - started <= timeout_seconds:
        registry = inspect_registry(
            helper_root=helper_root,
            registry_url=registry_url,
            package=package,
            version=version,
        )
        state = registry.get("state")
        if state == "available":
            _emit("publication_recovery_state", "available")
            return "available"
        if state == "yanked":
            raise PublicationRecoveryError(
                f"{package}@{version} is yanked; refusing publication recovery"
            )
        if state not in ABSENT_STATES:
            raise PublicationRecoveryError(
                f"unknown registry state {state!r}; refusing publication recovery"
            )

        runs = gh_json(
            [
                "run",
                "list",
                "--repo",
                repository,
                "--workflow",
                workflow,
                "--commit",
                version_commit,
                "--event",
                "push",
                "--limit",
                "100",
                "--json",
                (
                    "attempt,conclusion,createdAt,databaseId,event,"
                    "headBranch,headSha,status,url,workflowName"
                ),
            ],
            actions_token,
        )
        run = _run(
            runs,
            workflow=workflow,
            branch=default_branch,
            commit=version_commit,
        )
        if run is None:
            time.sleep(poll_seconds)
            continue
        status = run.get("status")
        if status != "completed":
            time.sleep(poll_seconds)
            continue
        conclusion = run.get("conclusion")
        run_id = run.get("databaseId")
        attempt = run.get("attempt")
        if not isinstance(run_id, int) or isinstance(run_id, bool):
            raise PublicationRecoveryError("registry run ID is invalid")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise PublicationRecoveryError(
                f"registry run {run_id} attempt is invalid: {attempt!r}"
            )
        if conclusion == "success":
            time.sleep(poll_seconds)
            continue
        if attempt >= max_attempts:
            raise PublicationRecoveryError(
                f"registry run {run_id} exhausted bounded attempt "
                f"{attempt}/{max_attempts}"
            )
        if conclusion in FAILED_JOB_RERUN_CONCLUSIONS:
            rerun_endpoint = "rerun-failed-jobs"
        elif conclusion in FULL_RERUN_CONCLUSIONS:
            rerun_endpoint = "rerun"
        else:
            raise PublicationRecoveryError(
                f"registry run {run_id} conclusion {conclusion!r} "
                "has no safe automatic rerun"
            )

        key = (run_id, attempt)
        first_seen = failed_seen.setdefault(key, time.monotonic())
        if time.monotonic() - first_seen < visibility_seconds:
            time.sleep(poll_seconds)
            continue
        if key in rerun_attempts:
            time.sleep(poll_seconds)
            continue

        print(
            f"registry row remains absent after visibility allowance; "
            f"requesting {rerun_endpoint} for exact run {run_id} at bounded "
            f"attempt {attempt + 1}/{max_attempts}",
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

    raise PublicationRecoveryError(
        f"{package}@{version} publication did not recover within "
        f"{timeout_seconds}s"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--version-commit", required=True)
    parser.add_argument("--workflow", default="Registry Publish")
    parser.add_argument("--default-branch", required=True)
    parser.add_argument("--registry-url", required=True)
    parser.add_argument("--helper-root", type=Path, required=True)
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(
            os.environ.get("KIN_PUBLICATION_RECOVERY_TIMEOUT_SECONDS", "900")
        ),
    )
    parser.add_argument(
        "--visibility-seconds",
        type=int,
        default=int(
            os.environ.get("KIN_REGISTRY_VISIBILITY_SECONDS", "60")
        ),
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=int(os.environ.get("KIN_PUBLICATION_POLL_SECONDS", "10")),
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=int(
            os.environ.get("KIN_PUBLICATION_MAX_ATTEMPTS", "3")
        ),
    )
    args = parser.parse_args(argv)
    token = os.environ.get("KIN_ACTIONS_TOKEN", "")
    if not token:
        print(
            "registry publication recovery refused: KIN_ACTIONS_TOKEN is required",
            file=sys.stderr,
        )
        return 1
    try:
        state = recover(
            repository=args.repository,
            package=args.package,
            version=args.version,
            version_commit=args.version_commit,
            workflow=args.workflow,
            default_branch=args.default_branch,
            registry_url=args.registry_url,
            helper_root=args.helper_root,
            actions_token=token,
            timeout_seconds=args.timeout_seconds,
            visibility_seconds=args.visibility_seconds,
            poll_seconds=args.poll_seconds,
            max_attempts=args.max_attempts,
        )
    except PublicationRecoveryError as exc:
        print(f"registry publication recovery refused: {exc}", file=sys.stderr)
        return 1
    print(f"registry publication recovery state: {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
