#!/usr/bin/env python3
"""Live workflow-run lookup guard.

A verification step that resolves a specific past Actions run is only as
durable as that run's retention. Deleting run history, by purge or by the
retention window, permanently breaks every check that reads a run id back,
and the break is silent until a release fails closed with no way to self-heal:
the run id is gone and will never resolve again. Immutable evidence, a tag, a
release asset digest, an image digest, or a signed attestation, does not decay
that way.

This guard fails when a tracked source file reaches for a specific workflow run
as a verification input:

  * ``repos/<owner>/<repo>/actions/runs/<id>`` and its sub-resources
    (``/jobs``, ``/artifacts``, ``/attempts/<n>/...``)
  * the workflow-scoped runs-list form ``actions/workflows/<name>/runs``
  * the equivalent API client calls (``getWorkflowRun``, ``listWorkflowRuns``,
    ``get_workflow_run``, and siblings)

Deliberately NOT flagged, because none of them is a retention-dependent
verification of a past run:

  * a lookup of the currently executing run (``GITHUB_RUN_ID`` /
    ``github.run_id``); it cannot be deleted while it is running
  * branch-ref compare-and-set checks (``git/ref/heads/...``)
  * the repo-wide runs list (``actions/runs?...``), which asks what is running
    now rather than resolving one past run
  * a human-facing run URL (``github.com/<owner>/<repo>/actions/runs/<id>``),
    which is a log link, not an input; the API path form always carries a
    ``repos/`` segment and that is what separates the two
  * full-line comments, so prose recording why a lookup was removed does not
    reopen the failure it describes

Consumers pass their own allowlist. Every entry carries a reason and an owner,
names a real path, and pins the exact expression it exempts. Validation never
short-circuits the scan: a malformed, unmatched, or stale entry grants no
exemption and is reported beside whatever the scan found, because an allowlist
error that skips the scan hides the true violation count behind a bookkeeping
mistake. That failure mode is why the two outcomes carry different exit codes.

Exit codes:

  0  no violations
  1  workflow-run lookups found (allowlist valid)
  3  allowlist invalid; the scan still ran and its findings are reported
  4  the guard could not run (not a Git checkout, or git failed)

Exit 3 outranks exit 1 when both happen: the allowlist has to be trustworthy
before a violation count means anything. Exit 2 is left to argparse for usage
errors.

Known limits, stated so a green run is not read as more than it is. This is a
static text scanner. It cannot follow a run id through a variable, so a path
assembled by concatenation (``"..." + repo + "/actions/runs/" + id`` in JS, or
a shell path split across ``api_base`` and a segment) is not matched, and both
are ordinary idioms rather than deliberate evasions. A GraphQL run query, a
base64-assembled path, and the numeric ``repositories/<id>/`` routing form are
likewise unmatched. The current-run exemption is decided by the name of the
expression and never by its value, so a workflow that deliberately assigns a
past run id to ``GITHUB_RUN_ID`` is exempted, and an alias holding
``github.run_id`` under a different name is flagged and needs an allowlist
entry saying so. Closing these in the regex would trade a large false-positive
surface for cases nothing in the fleet uses; the honest answer is that this
guard raises the cost of the common forms and does not claim to be a proof.

Usage: workflow-run-lookup-guard.py [--root DIR] [--allowlist FILE]
                                    [--include-tests]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date

EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_ALLOWLIST = 3
EXIT_RUN_ERROR = 4

# The file types that carry CI and verification logic across the fleet. `.js`
# and `.cjs` belong here beside `.mjs`: a CommonJS module talking to the GitHub
# API is the same policy surface, and enforcing one rule or not enforcing it
# based on a file suffix is the drift this checker exists to end.
SCANNED_EXTENSIONS = (".yml", ".yaml", ".sh", ".mjs", ".cjs", ".js", ".ts", ".py")

# The file types where a YAML-native cross-run form can appear.
YAML_EXTENSIONS = (".yml", ".yaml")

REQUIRED_FIELDS = ("file", "reason", "owner")
KNOWN_FIELDS = REQUIRED_FIELDS + ("allow_match", "allow_file", "expiration")

# One character of an API path, or a whole `${{ ... }}` expression. Actions
# expressions carry spaces inside the braces, so a character class alone stops
# at the first space and misses `repos/${{ github.repository }}/actions/runs/`,
# which is the same lookup written the other way.
PATH_ATOM = r"(?:\$\{\{[^}]*\}\}|[^\s\"'`])"

# The REST path form. The `repos/` segment is what distinguishes an API lookup
# from a human-facing run URL, which has no such segment. The middle is
# non-greedy so both a literal `owner/repo` and a single `${GITHUB_REPOSITORY}`
# expansion match.
RUN_BY_ID = re.compile(r"repos/" + PATH_ATOM + r"*?/actions/runs/")

# The workflow-scoped runs list. This form exists only as an API path; the
# human-facing workflow URL does not carry the trailing `/runs`.
RUNS_LIST = re.compile(r"actions/workflows/" + PATH_ATOM + r"*?/runs\b")

# Client-library equivalents, so moving the same lookup from a shell `gh api`
# call into Octokit or a Python client does not walk around the guard. The
# artifacts, logs, and usage siblings are here because the REST paths they wrap
# are flagged sub-resources; leaving them out enforced the same rule in one
# spelling and not the other.
CLIENT_METHODS = re.compile(
    r"\b(?:"
    r"getWorkflowRun|getWorkflowRunAttempt|getWorkflowRunUsage|"
    r"listWorkflowRuns|listWorkflowRunsForRepo|listWorkflowRunArtifacts|"
    r"listJobsForWorkflowRun|listJobsForWorkflowRunAttempt|"
    r"downloadWorkflowRunLogs|"
    r"get_workflow_run|get_workflow_run_attempt|get_workflow_run_usage|"
    r"list_workflow_runs|list_workflow_runs_for_repo|"
    r"list_workflow_run_artifacts|list_jobs_for_workflow_run|"
    r"download_workflow_run_logs"
    r")\b"
)

# The `gh run` CLI, which reaches a past run in fewer characters than `gh api`
# and is the cheapest way around a guard that only knows API paths. `gh run
# list` is deliberately absent: it asks what runs exist rather than resolving
# one.
GH_RUN_CLI = re.compile(
    r"\bgh\s+run\s+(?:view|download|watch|rerun|cancel|delete)\b"
)

# `actions/download-artifact` with `run-id:` retrieves an artifact from another
# run. It is a first-class GitHub feature, it carries no API path text at all,
# and it is written in YAML, so nothing else here would see it. The value is
# classified like any other run expression, so pulling this run's own artifacts
# stays allowed. An input declaration (`run-id:` with no value) does not match.
DOWNLOAD_RUN_ID = re.compile(r"(?:^|\s)run-id:\s*(?P<value>\S.*?)\s*$")

# The run id of the currently executing run, in the spellings CI actually uses.
# A run cannot be deleted while it is the one doing the asking, so reading its
# own id back is not the retention dependency this guard exists to stop.
SELF_RUN = re.compile(
    r"""^(?:
          \$\{\{\s*github\.run_id\s*\}\}
        | \$\{\{\s*env\.GITHUB_RUN_ID\s*\}\}
        | \$\{GITHUB_RUN_ID\}
        | \$GITHUB_RUN_ID(?![A-Za-z0-9_])
        | "\$GITHUB_RUN_ID"
    )""",
    re.VERBOSE,
)

# The run expression that follows `actions/runs/`, for the violation message.
RUN_EXPR = re.compile(
    r"^(\$\{\{[^}]*\}\}|\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*|[^\s\"'`/?)]*)"
)

CHECKER_PATH = os.path.realpath(__file__)


def comment_marker(rel_path):
    """Return the full-line comment marker for a scanned file type."""
    return "//" if rel_path.endswith((".mjs", ".cjs", ".js", ".ts")) else "#"


def non_comment_text(source, rel_path):
    """Return the file text minus its full-line comments.

    Allowlist validation counts occurrences over this rather than the raw file,
    so it sees exactly the lines the scanner will read.
    """
    marker = comment_marker(rel_path)
    return "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith(marker)
    )


def is_test_path(rel_path):
    """Report whether a path is a test or fixture by fleet naming convention.

    Test doubles and policy assertions quote the very shape they assert on, so
    scanning them would make fixing a production site impossible while its test
    still describes the old one. Skipped files are counted and printed rather
    than dropped silently, so the scan's real scope is always visible, and
    ``--include-tests`` scans them anyway.
    """
    parts = rel_path.split("/")
    if any(p in ("test", "tests", "__tests__", "fixtures") for p in parts[:-1]):
        return True
    name = parts[-1]
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return (
        name.startswith("test_")
        or name.startswith("test-")
        or stem.endswith("_test")
        or stem.endswith("-test")
        or ".test." in name
        or ".spec." in name
    )


def run_id_lookups(text):
    """Yield (expression, is_self) for each run-id path in one line.

    Shared by the scanner and by allowlist validation, so a pin is judged by
    exactly the rule that will later be applied to the line it exempts.
    """
    for match in RUN_BY_ID.finditer(text):
        tail = text[match.end():]
        yield RUN_EXPR.match(tail).group(1), bool(SELF_RUN.match(tail))


def line_findings(line, yaml_forms=True):
    """Return (reasons, self_hits) for one line of source.

    ``reasons`` is every distinct lookup form the line trips. ``self_hits``
    counts current-run lookups, which are allowed but still worth reporting so
    the summary never reads as "nothing here". ``yaml_forms`` enables the
    YAML-only cross-run artifact form.
    """
    reasons = []
    self_hits = 0

    for expression, is_self in run_id_lookups(line):
        if is_self:
            self_hits += 1
            continue
        reasons.append(f"workflow-run lookup by id ({expression or '<empty>'})")

    if RUNS_LIST.search(line):
        reasons.append("workflow-scoped runs-list lookup")

    for match in CLIENT_METHODS.finditer(line):
        reasons.append(f"workflow-run API client call ({match.group(0)})")

    if GH_RUN_CLI.search(line):
        reasons.append("gh run CLI call against a specific run")

    if yaml_forms:
        match = DOWNLOAD_RUN_ID.search(line)
        if match:
            value = match.group("value")
            if SELF_RUN.match(value):
                self_hits += 1
            else:
                reasons.append(f"cross-run artifact retrieval (run-id: {value})")

    return list(dict.fromkeys(reasons)), self_hits


def load_allowlist(path, root):
    """Return (exemptions, errors) without ever aborting the scan.

    ``exemptions`` maps a repo-relative path to the set of exact expressions
    exempt inside it, or to ``None`` when the whole file is exempt. Only valid
    entries are returned: a broken entry grants nothing, so a bookkeeping
    mistake can never widen an exemption or mask a violation.
    """
    exemptions = {}
    errors = []

    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as error:
        return exemptions, [f"cannot read allowlist {path}: {error}"]
    except json.JSONDecodeError as error:
        return exemptions, [f"allowlist {path} is not valid JSON: {error}"]

    if not isinstance(data, dict) or not isinstance(data.get("allowlist"), list):
        return exemptions, [
            f"allowlist {path} must be an object with an 'allowlist' array"
        ]

    today = date.today()

    for index, item in enumerate(data["allowlist"]):
        label = f"entry #{index}"
        if not isinstance(item, dict):
            errors.append(f"{label} is not an object")
            continue

        label = f"entry #{index} ({item.get('file', '?')})"

        unknown = sorted(set(item) - set(KNOWN_FIELDS))
        if unknown:
            errors.append(
                f"{label} has unknown field(s): {', '.join(unknown)}; "
                "a misspelled key must never widen an exemption"
            )
            continue

        missing = [k for k in REQUIRED_FIELDS if not item.get(k)]
        if missing:
            errors.append(f"{label} is missing required field(s): {', '.join(missing)}")
            continue

        rel_path = item["file"]
        if rel_path in exemptions:
            errors.append(f"{label} is duplicated; one file takes exactly one policy")
            continue

        if not rel_path.endswith(SCANNED_EXTENSIONS):
            errors.append(
                f"{label} names a file type this guard never scans "
                f"(scanned: {', '.join(SCANNED_EXTENSIONS)})"
            )
            continue

        source_path = os.path.join(root, rel_path)
        if not os.path.isfile(source_path):
            errors.append(
                f"{label} names a path that does not exist (owner: {item['owner']}); "
                "remove the stale entry, or a file later recreated at that path "
                "inherits the exemption unreviewed"
            )
            continue

        expiration = item.get("expiration")
        if expiration is not None:
            try:
                expires = date.fromisoformat(expiration)
            except (TypeError, ValueError):
                errors.append(
                    f"{label} has an invalid expiration {expiration!r} (want YYYY-MM-DD)"
                )
                continue
            if expires < today:
                errors.append(
                    f"{label} expired {expiration} (owner: {item['owner']}); "
                    "re-justify or remove it"
                )
                continue

        matches = item.get("allow_match")
        whole_file = item.get("allow_file")

        if whole_file is not None and not isinstance(whole_file, bool):
            errors.append(f"{label} has a non-boolean allow_file")
            continue
        if whole_file and matches:
            errors.append(
                f"{label} sets both allow_file and allow_match; "
                "pin the expressions or exempt the file, not both"
            )
            continue
        if not whole_file and matches is None:
            errors.append(
                f"{label} declares neither allow_match nor allow_file: true; "
                "a whole-file exemption has to be asked for explicitly"
            )
            continue

        if whole_file:
            exemptions[rel_path] = None
        else:
            if (
                not isinstance(matches, list)
                or not matches
                or not all(isinstance(m, str) and m and "\n" not in m for m in matches)
            ):
                errors.append(
                    f"{label} has an invalid allow_match "
                    "(want a non-empty list of non-empty single-line strings)"
                )
                continue
            if len(set(matches)) != len(matches):
                errors.append(f"{label} repeats an allow_match expression")
                continue
            try:
                with open(source_path, "r", encoding="utf-8", errors="replace") as handle:
                    source = handle.read()
            except OSError as error:
                errors.append(f"{label} could not be read: {error}")
                continue

            # Occurrences are counted over the lines the scanner actually
            # reads. Counting comment lines too would invalidate an entry the
            # moment someone documented the exempted expression beside it,
            # which is the guard's own recommended way to record why a lookup
            # is frozen.
            countable = non_comment_text(source, rel_path)

            bad_match = False
            for expression in matches:
                occurrences = countable.count(expression)
                if occurrences != 1:
                    bad_match = True
                    errors.append(
                        f"{label} allow_match {expression!r} occurs {occurrences} "
                        "time(s) on non-comment lines of that file (want exactly 1)"
                    )
                    continue
                reasons, _ = line_findings(expression)
                if not reasons:
                    bad_match = True
                    errors.append(
                        f"{label} allow_match {expression!r} contains no workflow-run "
                        "lookup, so it exempts nothing; pin the whole "
                        "'.../actions/runs/<expr>' span"
                    )
                    continue
                if any(not found for found, _ in run_id_lookups(expression)):
                    bad_match = True
                    errors.append(
                        f"{label} allow_match {expression!r} stops at 'actions/runs/' "
                        "and names no run id, so it would exempt whichever run that "
                        "line looks up later; pin the run expression too"
                    )
            if bad_match:
                continue
            exemptions[rel_path] = set(matches)

    return exemptions, errors


def mask(line, expressions):
    """Blank out exactly the allowlisted spans, keeping the rest authoritative.

    A second lookup beside an allowed one must still fail rather than inherit a
    whole-line exemption.
    """
    for expression in expressions:
        offset = line.find(expression)
        if offset >= 0:
            line = (
                line[:offset]
                + (" " * len(expression))
                + line[offset + len(expression):]
            )
    return line


def scan_file(abs_path, rel_path, expressions):
    """Return (violations, self_hits) for one file."""
    marker = comment_marker(rel_path)
    yaml_forms = rel_path.endswith(YAML_EXTENSIONS)
    violations = []
    self_hits = 0

    with open(abs_path, "r", encoding="utf-8", errors="replace") as handle:
        for number, raw in enumerate(handle, 1):
            line = raw.rstrip("\n")
            if line.lstrip().startswith(marker):
                continue
            if expressions:
                line = mask(line, expressions)
            reasons, hits = line_findings(line, yaml_forms)
            self_hits += hits
            if reasons:
                violations.append((number, raw.strip()[:160], ", ".join(reasons)))

    return violations, self_hits


def tracked_files(root):
    """Return the repo-relative tracked paths, or raise if git cannot answer."""
    result = subprocess.run(
        ["git", "-C", root, "ls-files", "-z"], text=True, capture_output=True
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "git ls-files failed").strip()
        raise RuntimeError(f"git ls-files failed in {root}: {detail}")
    return [path for path in result.stdout.split("\0") if path]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Fail on live workflow-run lookups used as verification inputs"
    )
    parser.add_argument("--root", default=".", help="repository root to scan")
    parser.add_argument(
        "--allowlist", default="", help="path to the JSON allowlist (optional)"
    )
    parser.add_argument(
        "--include-tests",
        action="store_true",
        help="also scan test and fixture paths, which are skipped by default",
    )
    args = parser.parse_args(argv)

    root = os.path.abspath(args.root)

    try:
        paths = tracked_files(root)
    except RuntimeError as error:
        print("Live workflow-run lookup guard")
        print(f"FAIL: {error}")
        print(f"exit: {EXIT_RUN_ERROR} (guard could not run)")
        return EXIT_RUN_ERROR

    exemptions = {}
    allowlist_errors = []
    if args.allowlist:
        exemptions, allowlist_errors = load_allowlist(args.allowlist, root)

    scanned = 0
    skipped_tests = 0
    skipped_self = 0
    skipped_whole_file = 0
    unskipped = []
    unreadable = []
    self_hits = 0
    findings = []

    for rel_path in sorted(paths):
        if not rel_path.endswith(SCANNED_EXTENSIONS):
            continue
        abs_path = os.path.join(root, rel_path)

        if os.path.realpath(abs_path) == CHECKER_PATH:
            skipped_self += 1
            continue

        expressions = None
        if rel_path in exemptions:
            expressions = exemptions[rel_path]
            if expressions is None:
                skipped_whole_file += 1
                continue
            # A pin on a test path pulls that file into the scan, which reads
            # as an exemption making the guard stricter. It is announced rather
            # than silent, so a newly surfaced sibling fixture line is traceable
            # to the entry that caused it.
            if not args.include_tests and is_test_path(rel_path):
                unskipped.append(rel_path)
        elif not args.include_tests and is_test_path(rel_path):
            skipped_tests += 1
            continue

        try:
            violations, hits = scan_file(abs_path, rel_path, expressions)
        except OSError as error:
            unreadable.append(f"{rel_path}: {error}")
            continue

        scanned += 1
        self_hits += hits
        for number, content, reason in violations:
            findings.append((rel_path, number, content, reason))

    print("Live workflow-run lookup guard")
    print(f"  root                 : {root}")
    print(f"  files scanned        : {scanned}")
    print(f"  skipped as tests     : {skipped_tests}"
          f"{' (--include-tests scans them)' if skipped_tests else ''}")
    print(f"  skipped as checker   : {skipped_self}")
    print(f"  allowlist            : {args.allowlist or '<none>'}")
    print(f"  allowlisted files    : {len(exemptions)} valid "
          f"({skipped_whole_file} whole-file)")
    print(f"  current-run lookups  : {self_hits} (allowed; not retention-dependent)")
    if unskipped:
        print(f"  test paths un-skipped: {len(unskipped)} (an allowlist entry "
              f"pulls a test path into the scan)")
        for rel_path in unskipped:
            print(f"    - {rel_path}")
    if unreadable:
        print(f"  unreadable files     : {len(unreadable)}")
        for detail in unreadable:
            print(f"    - {detail}")

    if findings:
        print("")
        print(f"VIOLATIONS: {len(findings)} line(s) reach for a specific workflow run:")
        for rel_path, number, content, reason in findings:
            print(f"  {rel_path}:{number}: {reason}")
            print(f"      {content}")
        print("")
        print("Verify from immutable evidence instead: a tag, a release asset digest,")
        print("an image digest, or a signed attestation. If a lookup genuinely must")
        print("stay, add an allowlist entry naming its reason, owner, and exact")
        print("expression.")

    if allowlist_errors:
        print("")
        print(f"ALLOWLIST ERRORS: {len(allowlist_errors)} invalid entry/entries.")
        print("The scan above still ran; a broken entry grants no exemption, so a")
        print("bookkeeping mistake cannot hide a real violation.")
        for detail in allowlist_errors:
            print(f"  - {detail}")
        print("")
        print(f"exit: {EXIT_ALLOWLIST} (allowlist error"
              f"{'; violations also found' if findings else ''})")
        return EXIT_ALLOWLIST

    if findings:
        print(f"exit: {EXIT_VIOLATIONS} (violations found)")
        return EXIT_VIOLATIONS

    print("PASS: no live workflow-run lookups outside the allowlist.")
    print(f"exit: {EXIT_OK}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
