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

Internal tracker references (``FIR-<n>``, ``linear.app`` links) are scoped by
whether the text becomes a commit message. On 2026-08-05 the founder reversed
the rule rather than the practice for anything that does: naming the tickets a
merge resolves is intended, and hundreds of commits already on public default
branches carry those refs from following that doctrine correctly. Barring them
also made the rule forbid its own output on a queue-managed repository, where
the squash message is minted from the pull request title and body.

So tracker references are allowed in **commit messages, the PR title, and the PR
body**, and still rejected in **added source content and branch names**, neither
of which becomes a commit message and both of which have standing rules the
reversal did not address. Do not widen or narrow that boundary without revisiting
the decision.

Typographic dashes follow the same minting boundary. Founder prose doctrine bars
the em dash from every written surface, and where the queue mints the squash
message from the title and body, a dash placed there is authored into public
history with nobody at the merge button. They are therefore rejected in **the PR
title and body** and left alone in commit messages and added content, which are
text already written rather than text minted at submit.

The gate is validation-only. It never rewrites history, timestamps, authors,
committers, or attribution. Authorship and attribution are outside its scope.
The legacy ``--check-timestamps`` option is accepted as a no-op for compatibility.

The detectors are pure functions (``scan_private_metadata``,
``scan_ticket_refs``, ``scan_branch_name``, ``scan_title``, ``scan_body``,
``scan_added_line``, ``scan_dashes``) and are unit-tested in
``scripts/test_history_hygiene.py`` with no network access.
"""
import argparse
import fnmatch
import re
import subprocess
import sys


# --- patterns ------------------------------------------------------------

# Internal tracker references. Rejected only where the text does not become a
# commit message; see the module docstring for the boundary and why it is there.
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

# Assistant attribution markers. These are scoped to the pull-request title and
# body alone, and deliberately not to commit messages or added content.
#
# The separation is not cosmetic. Commit-message and content scanning stayed
# out of attribution when the checks were made validation-only, because judging
# authorship on history already present is a different decision with its own
# fallout. The title and body are a different surface: on a queue-managed
# repository they are minted into the squash commit with nobody at the merge
# button, so a marker placed there is authored fresh at submit time and can
# still be edited by the person who wrote it.
ATTRIBUTION_PATTERNS = [
    ("assistant co-author trailer", re.compile(
        r"(?i)Co-Authored-By\s*:.*\b(?:Claude|Codex|ChatGPT|Copilot|Gemini|Cursor)\b")),
    ("assistant generation notice", re.compile(
        r"(?i)Generated\s+with\b.*\b(?:Claude|Codex|ChatGPT|Copilot|Gemini|Cursor)\b")),
    ("assistant vendor no-reply address", re.compile(
        r"(?i)\bnoreply@(?:anthropic|openai)\.com\b")),
]

# Typographic dashes, scoped to the pull-request title and body for the same
# reason attribution is. The character is written as an escape so this file
# carries none of the bytes it rejects.
DASH_PATTERNS = [
    ("em dash (U+2014)", re.compile("\u2014")),
    ("en dash (U+2013)", re.compile("\u2013")),
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


def scan_attribution(text):
    """Return labels for assistant attribution markers.

    Applied only to the PR title and body; see ``ATTRIBUTION_PATTERNS``.
    """
    return [label for label, pat in ATTRIBUTION_PATTERNS if pat.search(text or "")]


def scan_dashes(text):
    """Return labels for typographic dashes.

    Applied only to the PR title and body; see ``DASH_PATTERNS``.
    """
    return [label for label, pat in DASH_PATTERNS if pat.search(text or "")]


def scan_ticket_refs(text):
    """Return labels for internal tracker references in text.

    Applied only to surfaces that do not become a commit message: added source
    content and branch names.
    """
    out = []
    if TICKET_RE.search(text or ""):
        out.append("internal ticket ref (FIR-...)")
    if LINEAR_RE.search(text or ""):
        out.append("internal tracker link (linear.app)")
    return out


def scan_branch_name(ref):
    """Return reasons a branch name exposes a private reference.

    A branch name is not minted into any commit message, so it keeps the
    tracker-reference rule.
    """
    if not ref:
        return []
    return [f"branch '{ref}' contains {label}"
            for label in scan_ticket_refs(ref) + scan_private_metadata(ref)]


def scan_title(title):
    """Return reasons a PR title is unfit to be minted as the squash subject.

    The title is the squash subject, so tracker references are allowed here.
    Barring them would reproduce the rule forbidding its own output the first
    time someone titles a PR after the ticket it closes. A typographic dash is
    rejected, because the mint is the point past which it cannot be edited out.
    """
    if not title:
        return []
    return ([f"PR title contains private assistant-session metadata: {label}"
             for label in scan_private_metadata(title) + scan_attribution(title)]
            + [f"PR title contains a typographic dash: {label}"
               for label in scan_dashes(title)])


def scan_body(body):
    """Return reasons a PR body is unfit to be minted as the squash message.

    A body is a published artifact whether or not it becomes a commit, and on a
    queue-managed repository it becomes one verbatim, so it carries the
    assistant-session and dash rules and not the tracker-reference rule.
    """
    if not body:
        return []
    return ([f"PR body contains private assistant-session metadata: {label}"
             for label in scan_private_metadata(body) + scan_attribution(body)]
            + [f"PR body contains a typographic dash: {label}"
               for label in scan_dashes(body)])


def scan_added_line(line):
    """Return labels for private references in one added content line.

    Source content is not a commit message, so it keeps the tracker-reference
    rule: a ticket id belongs in the tracker, not in a durable code comment.
    """
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
    parser.add_argument("--body", default="", help="PR body (squash message)")
    parser.add_argument("--no-content", action="store_true",
                        help="skip scanning added source/doc lines")
    parser.add_argument("--no-commits", action="store_true",
                        help="skip scanning commit messages; with --no-content "
                             "the run touches no Git repository at all")
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
    commits = [] if args.no_commits else collect_commits(base, head)
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
    print(f"  scope            : {'<text only, no Git range>' if args.no_commits and args.no_content else scope}")
    # A skipped commit scan and a range that held no commits both leave the list
    # empty. Reporting them the same way is how a gate that inspected nothing
    # reads as a gate that found nothing.
    print("  commits scanned  : "
          + ("<skipped>" if args.no_commits else str(len(commits))))
    print(f"  branch           : {args.branch or '<none>'}")
    print(f"  title            : {len(args.title)} chars"
          if args.title else "  title            : <none supplied, not scanned>")
    print(f"  body             : {body_coverage}")
    print(f"  content scanning : {'off' if args.no_content else 'on'}")
    # The tracker rule applies to some surfaces and not others. A reader who
    # cannot see which is which reads a pass as broader than it is.
    print("  tracker refs     : rejected in added content and branch names; "
          "allowed in commit messages, title, body")
    print("  dashes           : em and en dashes rejected in the title and "
          "body; not scanned elsewhere")
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
