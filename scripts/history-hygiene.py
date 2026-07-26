#!/usr/bin/env python3
"""Public metadata safety gate.

Pre-merge check that prevents private references from entering shared/public
Git history:

  * Private assistant session trailers, identifiers, and URLs.
  * Internal ticket references (``FIR-<n>``, ``linear.app`` links) in commit
    messages, added content, the PR head branch name, or the squash subject.

The gate is validation-only. It never rewrites history, timestamps, authors,
committers, or attribution. Authorship and attribution are outside its scope.
The legacy ``--check-timestamps`` option is accepted as a no-op for compatibility.

The detectors are pure functions (``scan_private_metadata``,
``scan_ticket_refs``, ``scan_branch_name``, ``scan_added_line``) and are
unit-tested in ``scripts/test_history_hygiene.py`` with no network access.
"""
import argparse
import fnmatch
import re
import subprocess
import sys


# --- patterns ------------------------------------------------------------

# Internal tracker references that must not reach public history/content.
TICKET_RE = re.compile(r"\bFIR-\d+\b", re.IGNORECASE)
LINEAR_RE = re.compile(r"\blinear\.app/[^\s)\"']+", re.IGNORECASE)

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


def scan_ticket_refs(text):
    """Return labels for internal tracker references in text."""
    out = []
    if TICKET_RE.search(text or ""):
        out.append("internal ticket ref (FIR-...)")
    if LINEAR_RE.search(text or ""):
        out.append("internal tracker link (linear.app)")
    return out


def scan_branch_name(ref):
    """Return reasons a branch name exposes a private reference."""
    out = []
    if not ref:
        return out
    for label in scan_ticket_refs(ref):
        out.append(f"branch '{ref}' contains {label}")
    for label in scan_private_metadata(ref):
        out.append(f"branch '{ref}' contains {label}")
    return out


def scan_title(title):
    """Return reasons a PR title exposes a private reference."""
    out = []
    if not title:
        return out
    for label in scan_ticket_refs(title):
        out.append(f"PR title contains {label}: {title!r}")
    for label in scan_private_metadata(title):
        out.append(f"PR title contains private assistant-session metadata: {label}")
    return out


def scan_added_line(line):
    """Return labels for private references in one added content line."""
    return scan_ticket_refs(line) + scan_private_metadata(line)


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
        for label in scan_ticket_refs(c["body"]):
            violations.append(("internal tracker metadata", f"{short}: {label}"))

    # 2) branch name + PR title (the public squash subject)
    for reason in scan_branch_name(args.branch):
        violations.append(("branch/squash-subject leak", reason))
    for reason in scan_title(args.title):
        violations.append(("branch/squash-subject leak", reason))

    # 3) added source/doc content
    if not args.no_content:
        for path, line in collect_added_lines(base, head):
            if is_excluded(path, excludes):
                continue
            for label in scan_added_line(line):
                violations.append(("content leak", f"{path}: {label}: {line.strip()[:80]}"))

    scope = f"{base or '<root>'}..{head}"
    print("Public metadata safety gate")
    print(f"  scope            : {scope}")
    print(f"  commits scanned  : {len(commits)}")
    print(f"  branch           : {args.branch or '<none>'}")
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
