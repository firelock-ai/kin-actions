#!/usr/bin/env python3
"""Open, update, and close the single alarm issue for one scheduled workflow.

A workflow moved off pull requests and onto a cron has no reader. Nobody is
waiting on it, no pull request turns red, and the Actions tab shows a red run
that only somebody already looking would find. A job that skipped two hundred
times in this fleet read as green for exactly that reason. So a schedule
without an alarm is not a cheaper gate, it is a gate that has been removed.

This is the alarm. On a failing scheduled run it opens one issue for that
workflow and keeps updating it; on the next scheduled success it closes it.
The issue is the state, which is what makes the count honest and the noise
bounded:

  * one issue per workflow, never one per failure, so a streak escalates in
    place instead of burying the repository in duplicates
  * the consecutive count lives in a marker comment inside the body, so it
    survives without any database and without reading run history back
  * a redelivered or re-run event carrying a run id already recorded does not
    increment, so the count means distinct scheduled occurrences

Reading the count out of Actions history instead was the obvious design and is
barred here on purpose: a runs-list lookup decays with run retention, and this
repository's own workflow-run lookup guard refuses it. The issue does not
decay.

The recovery close is the half that is easy to leave out and expensive to lose.
An alarm that only ever opens teaches its readers to ignore it, so a green
scheduled run closes the issue and says which run cleared it.

Exit codes:

  0  the alarm acted, or correctly decided there was nothing to do
  1  a GitHub call failed, or the event could not be read

Never exit 0 on an API failure. A silent alarm is the defect this exists to
remove, so it fails loud and lets the alarm workflow itself go red.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys


MARKER_VERSION = "v1"
MARKER_PREFIX = f"kin-scheduled-failure-alarm:{MARKER_VERSION}"

# `--` cannot appear inside an HTML comment, so the key is sanitized to a
# character set that cannot produce it and runs are collapsed. A key that broke
# the comment would make the marker unparseable and every later failure would
# open a fresh issue, which is precisely the spam this design exists to avoid.
KEY_SAFE = re.compile(r"[^A-Za-z0-9._/]+")

MARKER_RE = re.compile(
    r"<!--\s*"
    + re.escape(MARKER_PREFIX)
    + r"\s+key=(?P<key>\S+)"
    r"\s+consecutive=(?P<consecutive>\d+)"
    r"\s+run=(?P<run>\S*)"
    r"\s*-->"
)

# Conclusions that mean the schedule did not prove anything and is broken. A
# cancelled or skipped run proves nothing either, but it is not evidence of
# breakage, so it neither raises nor clears the alarm; treating `skipped` as
# success is the exact mistake that let a skipped job read as green.
FAILING_CONCLUSIONS = ("failure", "timed_out", "startup_failure")
CLEARING_CONCLUSIONS = ("success",)

# GitHub caps an issue title at 256 characters.
TITLE_LIMIT = 256

# Bounded pagination. An alarm label collects a handful of issues, never
# thousands, and an unbounded walk would turn a mislabelled repository into a
# hang rather than a report.
MAX_PAGES = 10
PAGE_SIZE = 100


class AlarmError(RuntimeError):
    """An alarm invariant failed, or GitHub refused a call."""


def sanitize_key(value: str) -> str:
    """Return a marker-safe identity for one workflow.

    The workflow file path is preferred over its display name because renaming
    the workflow must not orphan its open alarm.
    """

    key = KEY_SAFE.sub("-", value.strip()).strip("-/")
    key = re.sub(r"-{2,}", "-", key)
    if not key:
        raise AlarmError(f"cannot derive an alarm key from {value!r}")
    return key


def render_marker(key: str, consecutive: int, run_id: str) -> str:
    marker = (
        f"<!-- {MARKER_PREFIX} key={key} "
        f"consecutive={consecutive} run={run_id or '0'} -->"
    )
    if "--" in marker[4:-3]:
        raise AlarmError(f"marker body would break the HTML comment: {marker!r}")
    return marker


def parse_marker(body: str) -> dict | None:
    """Return the alarm state carried by an issue body, or None."""

    match = MARKER_RE.search(body or "")
    if not match:
        return None
    return {
        "key": match.group("key"),
        "consecutive": int(match.group("consecutive")),
        "run": match.group("run"),
    }


def render_title(workflow_name: str, consecutive: int) -> str:
    plural = "" if consecutive == 1 else "s"
    suffix = f" ({consecutive} consecutive run{plural})"
    room = max(TITLE_LIMIT - len("Scheduled run failing: ") - len(suffix), 8)
    name = workflow_name
    if len(name) > room:
        name = name[: room - 3] + "..."
    return f"Scheduled run failing: {name}{suffix}"


def render_body(
    *,
    key: str,
    workflow_name: str,
    workflow_path: str,
    consecutive: int,
    conclusion: str,
    run_url: str,
    run_id: str,
    branch: str,
) -> str:
    plural = "" if consecutive == 1 else "s"
    lines = [
        f"**The scheduled run of `{workflow_name}` is failing.**",
        "",
        "This workflow runs on a schedule, so no pull request goes red when it "
        "breaks and nobody is waiting on the result. This issue is that missing "
        "reader. It is the only issue the alarm keeps for this workflow, it is "
        "updated in place rather than repeated, and it closes on its own the "
        "next time the schedule succeeds.",
        "",
        "| | |",
        "|---|---|",
        f"| Workflow | {workflow_name} |",
    ]
    if workflow_path:
        lines.append(f"| File | `{workflow_path}` |")
    lines.extend(
        [
            f"| Consecutive failed scheduled runs | **{consecutive}** |",
            f"| Latest conclusion | `{conclusion}` |",
            f"| Latest run | {run_url} |",
        ]
    )
    if branch:
        lines.append(f"| Branch | `{branch}` |")
    lines.extend(
        [
            "",
            f"The count is {consecutive} distinct scheduled run{plural} in a row, "
            "not a retry count. A run re-run under the same id does not raise it.",
            "",
            "Do not close this by hand while the schedule is still broken. A "
            "later failure reopens it and carries the count forward, so closing "
            "early loses the streak and buys nothing.",
            "",
            render_marker(key, consecutive, run_id),
        ]
    )
    return "\n".join(lines)


def render_cleared_body(
    *, key: str, workflow_name: str, workflow_path: str, consecutive: int,
    run_url: str, run_id: str,
) -> str:
    """Render the body of an alarm the schedule itself cleared.

    The marker drops to zero, and that zero is the whole point. A closed alarm
    has two possible histories: the schedule recovered, or a human closed it
    while the schedule was still broken. The first must start the next streak
    at one, the second must carry the old count forward. Without a recorded
    difference the alarm has to guess, and either guess is wrong half the time.
    """

    plural = "" if consecutive == 1 else "s"
    return "\n".join(
        [
            f"**Cleared.** The scheduled run of `{workflow_name}` is succeeding "
            "again.",
            "",
            f"This alarm was open for {consecutive} consecutive failed "
            f"run{plural} and the schedule then went green, so it closed itself. "
            "A new streak opens a new count here rather than resuming the old "
            "one.",
            "",
            "| | |",
            "|---|---|",
            f"| Workflow | {workflow_name} |",
        ]
        + ([f"| File | `{workflow_path}` |"] if workflow_path else [])
        + [
            f"| Streak that ended | {consecutive} |",
            f"| Clearing run | {run_url} |",
            "",
            render_marker(key, 0, run_id),
        ]
    )


def render_recovery_comment(*, workflow_name: str, run_url: str, consecutive: int) -> str:
    plural = "" if consecutive == 1 else "s"
    return (
        f"**Recovered.** The scheduled run of `{workflow_name}` succeeded, so "
        f"the alarm is closing itself after {consecutive} consecutive failed "
        f"run{plural}.\n\n"
        f"Clearing run: {run_url}\n\n"
        "Nothing here was verified beyond the schedule going green. If the "
        "failure was intermittent, the next streak reopens this alarm with a "
        "fresh count."
    )


def gh_api(args: list[str], *, method: str = "GET", fields: dict | None = None) -> object:
    """Call `gh api` and return parsed JSON, or raise AlarmError.

    Every value reaches gh as argv rather than through a shell, so a workflow
    name or branch carrying shell metacharacters is data and never code.
    """

    command = ["gh", "api", "--method", method, *args]
    for name, value in (fields or {}).items():
        command.extend(["-f", f"{name}={value}"])
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise AlarmError(
            f"gh api {method} {' '.join(args)} failed"
            + (f": {detail}" if detail else "")
        )
    if not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AlarmError(f"gh api returned non-JSON output: {exc}") from exc


class GitHub:
    """The GitHub calls this alarm makes, and nothing else.

    Isolated behind one class so the tests can drive every branch of the state
    machine against a double. A state machine only exercised through a live API
    is a state machine whose recovery path never runs until it matters.
    """

    def __init__(self, repository: str, api=None):
        self.repository = repository
        # Resolved at call time rather than captured as a default argument. A
        # default binds at class-definition time, so a test that replaces the
        # transport would still reach the live API and pass on a 404 instead of
        # on the behaviour it meant to assert.
        self._api = api

    def api(self, *args, **kwargs):
        return (self._api or gh_api)(*args, **kwargs)

    def list_labelled_issues(self, label: str) -> list[dict]:
        issues: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            batch = self.api(
                [
                    f"repos/{self.repository}/issues"
                    f"?state=all&labels={label}&per_page={PAGE_SIZE}"
                    f"&sort=updated&direction=desc&page={page}"
                ]
            )
            if not isinstance(batch, list):
                raise AlarmError("issue listing is not a JSON array")
            issues.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
        return issues

    def ensure_label(self, label: str, color: str, description: str) -> None:
        try:
            self.api(
                [f"repos/{self.repository}/labels"],
                method="POST",
                fields={"name": label, "color": color, "description": description},
            )
        except AlarmError as exc:
            # A label that already exists is the normal case after the first
            # alarm. Anything else is a real failure and must not be hidden,
            # because a token that cannot write labels usually cannot write
            # issues either, and that is the alarm being silently disarmed.
            if "already_exists" not in str(exc):
                raise

    def create_issue(self, title: str, body: str, label: str) -> dict:
        created = self.api(
            [f"repos/{self.repository}/issues", "-f", f"labels[]={label}"],
            method="POST",
            fields={"title": title, "body": body},
        )
        if not isinstance(created, dict):
            raise AlarmError("issue creation returned no issue object")
        return created

    def update_issue(self, number: int, **fields: str) -> None:
        self.api(
            [f"repos/{self.repository}/issues/{number}"],
            method="PATCH",
            fields=fields,
        )

    def comment(self, number: int, body: str) -> None:
        self.api(
            [f"repos/{self.repository}/issues/{number}/comments"],
            method="POST",
            fields={"body": body},
        )


def find_alarm_issue(github: GitHub, label: str, key: str) -> dict | None:
    """Return the newest issue whose marker claims this workflow.

    The issues REST listing is used rather than the search API on purpose. The
    search index is eventually consistent, and an alarm that cannot see the
    issue it opened a minute ago opens a second one, which is the duplicate
    storm this design exists to prevent.
    """

    best = None
    for issue in github.list_labelled_issues(label):
        if not isinstance(issue, dict):
            raise AlarmError("issue listing carries a non-object entry")
        # The issues endpoint returns pull requests too. A PR body carrying
        # this marker must never be mistaken for the alarm issue and patched.
        if issue.get("pull_request"):
            continue
        state = parse_marker(issue.get("body") or "")
        if not state or state["key"] != key:
            continue
        if best is None:
            best = (issue, state)
        elif issue.get("state") == "open" and best[0].get("state") != "open":
            best = (issue, state)
    if best is None:
        return None
    issue, state = best
    return {
        "number": issue.get("number"),
        "state": issue.get("state"),
        "consecutive": state["consecutive"],
        "run": state["run"],
    }


def decide(*, conclusion: str, event: str, alarm_events: list[str]) -> str:
    """Classify one completed workflow run as raise, clear, or ignore."""

    if event not in alarm_events:
        return "ignore-event"
    if conclusion in FAILING_CONCLUSIONS:
        return "raise"
    if conclusion in CLEARING_CONCLUSIONS:
        return "clear"
    return "ignore-conclusion"


def run(args: argparse.Namespace, github: GitHub) -> int:
    alarm_events = [e.strip() for e in args.alarm_events.split(",") if e.strip()]
    if not alarm_events:
        raise AlarmError("--alarm-events resolved to an empty list")

    action = decide(
        conclusion=args.conclusion, event=args.event, alarm_events=alarm_events
    )
    key = sanitize_key(args.workflow_path or args.workflow_name)

    if action == "ignore-event":
        print(
            f"run event {args.event!r} is not one of {alarm_events}; "
            "this alarm covers scheduled runs only"
        )
        return 0
    if action == "ignore-conclusion":
        print(
            f"conclusion {args.conclusion!r} neither raises nor clears the alarm "
            f"(raising: {list(FAILING_CONCLUSIONS)}, "
            f"clearing: {list(CLEARING_CONCLUSIONS)})"
        )
        return 0

    existing = find_alarm_issue(github, args.label, key)

    if action == "clear":
        if not existing or existing["state"] != "open":
            print(f"no open alarm for {key}; scheduled success needs no action")
            return 0
        if not args.dry_run:
            github.comment(
                existing["number"],
                render_recovery_comment(
                    workflow_name=args.workflow_name,
                    run_url=args.run_url,
                    consecutive=existing["consecutive"],
                ),
            )
            github.update_issue(
                existing["number"],
                state="closed",
                state_reason="completed",
                body=render_cleared_body(
                    key=key,
                    workflow_name=args.workflow_name,
                    workflow_path=args.workflow_path,
                    consecutive=existing["consecutive"],
                    run_url=args.run_url,
                    run_id=str(args.run_id),
                ),
            )
        print(
            f"closed alarm #{existing['number']} for {key} after "
            f"{existing['consecutive']} consecutive failure(s)"
        )
        return 0

    if existing and existing["run"] and existing["run"] == str(args.run_id):
        print(
            f"alarm #{existing['number']} already records run {args.run_id}; "
            f"leaving the count at {existing['consecutive']}"
        )
        return 0

    consecutive = (existing["consecutive"] + 1) if existing else 1
    title = render_title(args.workflow_name, consecutive)
    body = render_body(
        key=key,
        workflow_name=args.workflow_name,
        workflow_path=args.workflow_path,
        consecutive=consecutive,
        conclusion=args.conclusion,
        run_url=args.run_url,
        run_id=str(args.run_id),
        branch=args.branch,
    )

    if existing:
        if not args.dry_run:
            fields = {"title": title, "body": body}
            if existing["state"] != "open":
                fields["state"] = "open"
            github.update_issue(existing["number"], **fields)
        print(
            f"updated alarm #{existing['number']} for {key}: "
            f"{consecutive} consecutive failure(s)"
        )
        return 0

    if args.dry_run:
        print(f"would open an alarm for {key}: 1 consecutive failure")
        return 0
    github.ensure_label(
        args.label,
        args.label_color,
        "A scheduled workflow is failing with nobody waiting on it",
    )
    created = github.create_issue(title, body, args.label)
    print(f"opened alarm #{created.get('number')} for {key}: 1 consecutive failure")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Raise or clear the single alarm issue for a scheduled workflow"
    )
    parser.add_argument("--repository", required=True, help="OWNER/NAME")
    parser.add_argument("--workflow-name", required=True)
    parser.add_argument("--workflow-path", default="")
    parser.add_argument("--conclusion", required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--run-url", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--branch", default="")
    parser.add_argument("--label", default="scheduled-failure")
    parser.add_argument("--label-color", default="B60205")
    parser.add_argument("--alarm-events", default="schedule")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    owner, separator, name = args.repository.partition("/")
    if not separator or not owner or not name or "/" in name:
        print(f"repository must be OWNER/NAME, got {args.repository!r}", file=sys.stderr)
        return 1
    try:
        return run(args, GitHub(args.repository))
    except AlarmError as exc:
        print(f"::error::scheduled-failure alarm could not act: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
