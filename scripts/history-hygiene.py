#!/usr/bin/env python3
"""Public metadata safety gate.

Pre-merge check that prevents private assistant-session trailers, identifiers,
and URLs from entering shared/public Git history. They are rejected in commit
messages, in added content, in the PR head branch name, and in the squash
subject and body.

Where a repository sets ``squash_merge_commit_message: PR_BODY``, the merge
queue mints the squash commit message from the pull request body with nobody at
the merge button, so the body is not review prose: it is the commit message.
``--body`` is therefore checked at pull-request time, where a violation reports
on the PR and can be fixed, rather than reaching public history at the merge.

Internal tracker references (``FIR-<n>``, ``linear.app`` links) are NOT a
violation. The gate barred them until 2026-08-05, when the founder reversed the
rule rather than the practice: naming the tickets a merge resolves is intended,
and hundreds of commits already on public default branches carry those refs by
following that doctrine correctly. Nothing here should reintroduce the bar
without that decision being revisited.

The gate is validation-only. It never rewrites history, timestamps, authors,
committers, or attribution. Authorship and attribution are outside its scope.
The legacy ``--check-timestamps`` option is accepted as a no-op for compatibility.

The detectors are pure functions (``scan_private_metadata``,
``scan_branch_name``, ``scan_title``, ``scan_body``, ``scan_added_line``) and are
unit-tested in ``scripts/test_history_hygiene.py`` with no network access.
"""
import argparse
import fnmatch
import re
import subprocess
import sys


# --- patterns ------------------------------------------------------------

# Private assistant-session references. Attribution is outside this scanner.
PRIVATE_METADATA_PATTERNS = [
    # Unanchored so comment-embedded private session references in source
    # content are caught, not just line-start commit trailers.
    ("assistant session trailer", re.compile(
        r"(?i)\b(?:Claude|Codex|OpenAI|ChatGPT|Gemini|Copilot|Cursor)-Session\s*:")),
    (
        "assistant session URL",
        re.compile(r"(?i)https?://(?:claude\.ai|chat\.openai\.com|chatgpt\.com)/\S+"),
    ),
    ("assistant session id", re.compile(r"\bsession_[A-Za-z0-9]{16,}\b")),
]

# Only the scanner and its unit-test fixture are excluded because they contain
# the patterns by design. Public workflows, changelogs, and lockfiles are scanned.
DEFAULT_CONTENT_EXCLUDES = [
    "scripts/history-hygiene.py",
    "scripts/test_history_hygiene.py",
]


# --- pure detectors ------------------------------------------------------

def scan_private_metadata(text):
    """Return labels for private assistant-session references."""
    return [label for label, pat in PRIVATE_METADATA_PATTERNS if pat.search(text or "")]


def scan_message_ai(text):
    """Compatibility alias for callers of the former detector name."""
    return scan_private_metadata(text)


def scan_branch_name(ref):
    """Return reasons a branch name exposes a private reference."""
    if not ref:
        return []
    return [f"branch '{ref}' contains {label}" for label in scan_private_metadata(ref)]


def scan_title(title):
    """Return reasons a PR title exposes a private reference."""
    if not title:
        return []
    return [f"PR title contains private assistant-session metadata: {label}"
            for label in scan_private_metadata(title)]


def scan_body(body):
    """Return reasons a PR body exposes a private reference.

    A body is a published artifact whether or not it becomes a commit, and on a
    queue-managed repository it becomes one verbatim.
    """
    if not body:
        return []
    return [f"PR body contains private assistant-session metadata: {label}"
            for label in scan_private_metadata(body)]


def scan_added_line(line):
    """Return labels for private references in one added content line."""
    return scan_private_metadata(line)


def is_excluded(path, excludes):
    return any(fnmatch.fnmatch(path, pattern) for pattern in excludes)


# --- git plumbing --------------------------------------------------------

_REC = "\0\0"
_FLD = "\0"


def _git(args):
    result = subprocess.run(["git", *args], text=True, capture_output=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "git command failed").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result


def collect_commits(base, head):
    # NUL is forbidden in Git commit headers/messages, so NUL-delimited fields
    # cannot be forged by a commit message to truncate or skip its own scan.
    # `-z` adds a second NUL between records because the format ends in one.
    # Keep Git's `%x00` escapes literal in argv. Passing an actual NUL in an
    # argument is rejected by the operating system before Git can run.
    fmt = "%H%x00%B%x00"
    if base:
        rng = f"{base}..{head}"
        result = _git(["log", "-z", f"--format={fmt}", rng])
    else:
        result = _git(["log", "-z", f"--format={fmt}", "-1", head])
    commits = []
    for record in result.stdout.split(_REC):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split(_FLD)
        if len(parts) < 2:
            continue
        commits.append({
            "sha": parts[0],
            "body": parts[1],
        })
    return commits


def collect_added_lines(base, head):
    if base:
        args = ["diff", "--no-color", "--unified=0", f"{base}...{head}"]
    else:
        args = ["show", "--no-color", "--unified=0", "--format=", head]
    result = _git(args)
    path = None
    added = []
    for line in result.stdout.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("+++ "):
            path = None
        elif line.startswith("+") and not line.startswith("+++"):
            if path:
                added.append((path, line[1:]))
    return added


# --- main ----------------------------------------------------------------

def _is_zero(ref):
    return not ref or set(ref) == {"0"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Public metadata safety gate")
    parser.add_argument("--base", default="", help="base ref/sha (merge-base side)")
    parser.add_argument("--head", default="HEAD", help="head ref/sha")
    parser.add_argument("--branch", default="", help="PR head branch name")
    parser.add_argument("--title", default="", help="PR title (squash subject)")
    parser.add_argument("--body", default="", help="PR body (squash message)")
    parser.add_argument("--no-content", action="store_true",
                        help="skip scanning added source/doc lines")
    parser.add_argument("--check-timestamps", action="store_true",
                        help="deprecated compatibility option; ignored")
    parser.add_argument("--bot-email", action="append", default=[],
                        help=argparse.SUPPRESS)
    parser.add_argument("--tz", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--window-start", type=int, default=None,
                        help=argparse.SUPPRESS)
    parser.add_argument("--window-end", type=int, default=None,
                        help=argparse.SUPPRESS)
    parser.add_argument("--content-exclude", action="append", default=[],
                        help="extra glob excluded from content scanning")
    args = parser.parse_args(argv)

    base = "" if _is_zero(args.base) else args.base
    head = args.head or "HEAD"
    excludes = DEFAULT_CONTENT_EXCLUDES + list(args.content_exclude)

    violations = []  # (category, detail)

    # 1) private references in commit messages
    commits = collect_commits(base, head)
    for c in commits:
        short = c["sha"][:12]
        for label in scan_private_metadata(c["body"]):
            violations.append(("private metadata", f"{short}: {label}"))

    # 2) branch name + PR title/body (the public squash subject and message)
    for reason in scan_branch_name(args.branch):
        violations.append(("branch/squash-subject leak", reason))
    for reason in scan_title(args.title):
        violations.append(("branch/squash-subject leak", reason))
    for reason in scan_body(args.body):
        violations.append(("branch/squash-subject leak", reason))

    # 3) added source/doc content
    if not args.no_content:
        for path, line in collect_added_lines(base, head):
            if is_excluded(path, excludes):
                continue
            for label in scan_added_line(line):
                violations.append(("content leak", f"{path}: {label}: {line.strip()[:80]}"))

    scope = f"{base or '<root>'}..{head}"
    # Report every input's coverage, not just the verdict. A body that never
    # arrived and a body that scanned clean both produce zero violations, and
    # the difference between them is the whole of FIR-1965.
    if not args.body:
        body_coverage = "<none supplied, not scanned>"
    else:
        body_coverage = f"{len(args.body)} chars scanned as the squash message"
    print("Public metadata safety gate")
    print(f"  scope            : {scope}")
    print(f"  commits scanned  : {len(commits)}")
    print(f"  branch           : {args.branch or '<none>'}")
    print(f"  title            : {len(args.title)} chars"
          if args.title else "  title            : <none supplied, not scanned>")
    print(f"  body             : {body_coverage}")
    print(f"  content scanning : {'off' if args.no_content else 'on'}")
    if args.check_timestamps:
        print("  legacy timestamp option: ignored")

    if not violations:
        print("OK: no private reference violations found.")
        return 0

    print(f"FAIL: {len(violations)} metadata safety violation(s):")
    # Group by category for readability.
    by_cat = {}
    for category, detail in violations:
        by_cat.setdefault(category, []).append(detail)
    for category in sorted(by_cat):
        print(f"  [{category}]")
        for detail in by_cat[category][:25]:
            print(f"    - {detail}")
        if len(by_cat[category]) > 25:
            print(f"    ... {len(by_cat[category]) - 25} more")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
