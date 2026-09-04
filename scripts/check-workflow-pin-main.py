#!/usr/bin/env python3
"""Require the four-job main proof for a Cargo consumer pin rollout."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys


SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
EXPECTED_JOBS = {
    "release / Registry-only build": "success",
    "release / Repo verification": "success",
    "release / Publish to Kin registry": "skipped",
    "release / Fresh consumer smoke": "skipped",
}


class MainProofError(RuntimeError):
    """The exact landed-main workflow evidence is malformed or terminally red."""


def evaluate_main_proof(
    *,
    repository: str,
    branch: str,
    head_sha: str,
    runs: list[dict],
    jobs_by_run: dict[int, list[dict]],
) -> dict:
    exact = [
        run
        for run in runs
        if run.get("head_sha") == head_sha
        and run.get("head_branch") == branch
        and run.get("event") == "push"
        and isinstance(run.get("repository"), dict)
        and run["repository"].get("full_name") == repository
        and isinstance(run.get("head_repository"), dict)
        and run["head_repository"].get("full_name") == repository
    ]
    if not exact:
        return {"status": "waiting", "reason": "no exact-SHA push run"}
    exact.sort(key=lambda run: int(run.get("id", 0)), reverse=True)
    run = exact[0]
    if run.get("status") != "completed":
        return {"status": "waiting", "reason": "exact-SHA push run is incomplete"}
    if run.get("conclusion") != "success":
        raise MainProofError(
            f"exact-SHA push run concluded {run.get('conclusion')!r}"
        )
    run_id = run.get("id")
    if not isinstance(run_id, int):
        raise MainProofError("exact-SHA push run has no numeric id")
    jobs = jobs_by_run.get(run_id, [])
    latest: dict[str, str | None] = {}
    for job in jobs:
        name = job.get("name")
        if name in EXPECTED_JOBS:
            if name in latest:
                raise MainProofError(f"duplicate protected release job: {name}")
            latest[name] = job.get("conclusion")
    missing = sorted(set(EXPECTED_JOBS) - set(latest))
    if missing:
        raise MainProofError("exact-SHA run lacks release jobs: " + ", ".join(missing))
    wrong = [
        f"{name}={latest[name]!r}"
        for name, conclusion in EXPECTED_JOBS.items()
        if latest[name] != conclusion
    ]
    if wrong:
        raise MainProofError("protected release job conclusions differ: " + ", ".join(wrong))
    return {"status": "proven", "run_id": run_id, "head_sha": head_sha}


def _json_command(command: list[str]):
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise MainProofError(
            f"command failed ({result.returncode}): {' '.join(command)}: "
            f"{result.stderr.strip()}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MainProofError(
            f"command returned invalid JSON: {' '.join(command)}"
        ) from exc


def fetch_evidence(repository: str, workflow: str, branch: str, head_sha: str):
    response = _json_command(
        [
            "gh",
            "api",
            "--method",
            "GET",
            f"repos/{repository}/actions/workflows/{workflow}/runs",
            "-f",
            "event=push",
            "-f",
            f"branch={branch}",
            "-f",
            f"head_sha={head_sha}",
            "-f",
            "per_page=100",
        ]
    )
    runs = response.get("workflow_runs") if isinstance(response, dict) else None
    if not isinstance(runs, list):
        raise MainProofError("workflow-run query returned invalid JSON")
    jobs_by_run: dict[int, list[dict]] = {}
    for run in runs:
        run_id = run.get("id")
        if not isinstance(run_id, int):
            continue
        pages = _json_command(
            [
                "gh",
                "api",
                "--paginate",
                "--slurp",
                f"repos/{repository}/actions/runs/{run_id}/jobs?per_page=100",
            ]
        )
        jobs: list[dict] = []
        for page in pages:
            if not isinstance(page, dict) or not isinstance(page.get("jobs"), list):
                raise MainProofError("workflow-job pagination returned an invalid page")
            jobs.extend(page["jobs"])
        jobs_by_run[run_id] = jobs
    return runs, jobs_by_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--head-sha", required=True)
    args = parser.parse_args(argv)
    if not os.environ.get("GH_TOKEN"):
        print("workflow-pin main proof failed: GH_TOKEN is required", file=sys.stderr)
        return 1
    if (
        not REPOSITORY_RE.fullmatch(args.repository)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+\.ya?ml", args.workflow)
        or not SHA_RE.fullmatch(args.head_sha)
    ):
        print("workflow-pin main proof failed: unsafe repository, workflow, or SHA", file=sys.stderr)
        return 1
    try:
        runs, jobs = fetch_evidence(
            args.repository, args.workflow, args.branch, args.head_sha
        )
        result = evaluate_main_proof(
            repository=args.repository,
            branch=args.branch,
            head_sha=args.head_sha,
            runs=runs,
            jobs_by_run=jobs,
        )
    except MainProofError as exc:
        print(f"workflow-pin main proof failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
