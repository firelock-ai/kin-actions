#!/usr/bin/env python3
"""Public-history hygiene gate.

Pre-merge check that fails a change before it can leak non-public-clean
metadata into shared/public git history. It mirrors the firelock commit
guard so CI rejects the same things the local hooks scrub:

  * AI-authorship metadata in commit messages or author/committer identity
    (assistant session trailers, AI ``Co-authored-by`` lines, "Generated
    with/by <assistant>" lines, assistant session ids and assistant URLs).
  * Internal ticket references (``FIR-<n>``, ``linear.app`` links) added to
    public-facing source/doc lines, and in the PR head branch name / squash
    subject (PR title) where they become a public squash-merge subject.
  * Branch names that carry an AI tool name into the merge subject.

Optionally (``--check-timestamps``, off by default) it flags restricted-window
commit timestamps (Mon-Fri 09:00-17:59 America/New_York), exempting ``[bot]``
author identities -- the same window and bot exemption the firelock guard uses.

The detectors are pure functions (``scan_message_ai``, ``scan_identity_ai``,
``scan_ticket_refs``, ``scan_branch_name``, ``scan_added_line``,
``restricted_window_violation``) and are unit-tested in
``scripts/test_history_hygiene.py`` -- no git or network needed.
"""
import argparse
import datetime as dt
import fnmatch
import re
import subprocess
import sys

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - py<3.9
    ZoneInfo = None


# --- patterns ------------------------------------------------------------

# Internal tracker references that must not reach public history/content.
TICKET_RE = re.compile(r"\bFIR-\d+\b", re.IGNORECASE)
LINEAR_RE = re.compile(r"\blinear\.app/[^\s)\"']+", re.IGNORECASE)

# AI-authorship traces. (label, compiled regex) so violations are explainable.
AI_TRACE_PATTERNS = [
    # Unanchored so comment-embedded traces (e.g. `# Claude-Session: ...`) in
    # source content are caught, not just line-start commit trailers.
    ("assistant session trailer", re.compile(
        r"(?i)\b(?:Claude|Codex|OpenAI|ChatGPT|Gemini|Copilot|Cursor)-Session\s*:")),
    ("AI co-author trailer", re.compile(
        r"(?i)Co-authored-by:[^\n]*\b(?:claude|anthropic|codex|openai|chatgpt|"
        r"copilot|cursor|gemini|devin|aider)\b")),
    ("anthropic no-reply co-author", re.compile(
        r"(?i)Co-authored-by:[^\n]*<[^>]*noreply@anthropic\.com>")),
    ("AI generation trailer", re.compile(
        r"(?i)Generated\s+(?:with|by)\s+\[?(?:Claude|Codex|GPT|ChatGPT|Copilot|"
        r"Cursor|Gemini|Anthropic)\b")),
    ("assistant generation marker", re.compile(r"🤖\s*Generated with")),
    ("assistant session URL", re.compile(r"(?i)https?://(?:claude\.ai|chat\.openai\.com)/\S+")),
    ("assistant session id", re.compile(r"\bsession_[A-Za-z0-9]{16,}\b")),
    ("AI attribution trailer", re.compile(
        r"(?i)\b(?:AI-Generated(?:-by)?|Assisted-by)\s*:")),
]

# AI tool names that must not appear as a token in author/committer identity.
AI_IDENTITY_RE = re.compile(
    r"(?i)\b(claude|anthropic|codex|openai|chatgpt|copilot|cursor|gemini|devin|aider)\b")

# AI tool names that must not be carried by a branch into the merge subject.
# Mirrors firelock_guard BLOCKED_BRANCH_PATTERNS.
AI_TOOL_BRANCH_RE = re.compile(
    r"(?:^|[/\-_])(codex|claude|copilot|cursor|gpt|openai|chatgpt|devin|aider|"
    r"autopilot|anthropic|gemini|codeium|tabnine|sourcegraph|cody|windsurf|"
    r"bolt|v0|lovable|replit)(?:[/\-_]|$)", re.IGNORECASE)

# Restricted-window policy (America/New_York, Mon-Fri 09:00-17:59) -- matches
# firelock_guard WINDOW_START_HOUR/WINDOW_END_HOUR.
DEFAULT_TZ = "America/New_York"
DEFAULT_WINDOW_START = 9
DEFAULT_WINDOW_END = 18

# Default identities exempted from author/timestamp checks: server-side bots.
DEFAULT_BOT_EMAILS = {
    "41898282+github-actions[bot]@users.noreply.github.com",
    "actions@github.com",
    "noreply@github.com",
}

# Paths excluded from added-line content scanning by default: CI config and the
# gate's own tooling/tests/fixtures (which legitimately contain the patterns),
# changelogs, and lockfiles.
DEFAULT_CONTENT_EXCLUDES = [
    ".github/*",
    "*history-hygiene*",
    "*check-version-bump*",
    "*test_*.py",
    "*.lock",
    "CHANGELOG*",
    "*/CHANGELOG*",
]


# --- pure detectors ------------------------------------------------------

def scan_message_ai(text):
    """Return labels for any AI-authorship traces in a commit message/line."""
    return [label for label, pat in AI_TRACE_PATTERNS if pat.search(text or "")]


def scan_identity_ai(name, email):
    """Return a reason if author/committer identity names an AI tool."""
    name = name or ""
    email = (email or "")
    if AI_IDENTITY_RE.search(name) or "noreply@anthropic.com" in email.lower():
        return [f"AI tool in identity: {name} <{email}>"]
    return []


def scan_ticket_refs(text):
    """Return labels for internal tracker references in text."""
    out = []
    if TICKET_RE.search(text or ""):
        out.append("internal ticket ref (FIR-...)")
    if LINEAR_RE.search(text or ""):
        out.append("internal tracker link (linear.app)")
    return out


def scan_branch_name(ref):
    """Return reasons a branch name would pollute a public squash subject."""
    out = []
    if not ref:
        return out
    if TICKET_RE.search(ref):
        out.append(f"branch '{ref}' leaks an internal ticket ref into the merge subject")
    if AI_TOOL_BRANCH_RE.search(ref):
        out.append(f"branch '{ref}' carries an AI tool name into the merge subject")
    return out


def scan_title(title):
    """Return reasons a PR title (the squash subject) is not public-clean."""
    out = []
    if not title:
        return out
    if TICKET_RE.search(title):
        out.append(f"PR title leaks an internal ticket ref: {title!r}")
    for label in scan_message_ai(title):
        out.append(f"PR title contains AI-authorship metadata: {label}")
    return out


def scan_added_line(line):
    """Return labels for ticket refs / AI traces in a single added content line."""
    return scan_ticket_refs(line) + scan_message_ai(line)


def is_excluded(path, excludes):
    return any(fnmatch.fnmatch(path, pattern) for pattern in excludes)


def restricted_window_violation(epoch, author_email, *, tz=DEFAULT_TZ,
                                start_hour=DEFAULT_WINDOW_START,
                                end_hour=DEFAULT_WINDOW_END, bot_emails=None):
    """Return the offending datetime if a commit falls in the restricted window.

    Server-side bots (``[bot]`` in the email, or an explicitly allowed bot
    address) are exempt, mirroring the firelock guard -- their timestamps are
    created in CI and never pass through the local normalizing hooks.
    """
    bot_emails = bot_emails or DEFAULT_BOT_EMAILS
    email = (author_email or "").lower()
    if "[bot]" in email or email in {b.lower() for b in bot_emails}:
        return None
    if ZoneInfo is None:
        return None
    when = dt.datetime.fromtimestamp(int(epoch), ZoneInfo(tz))
    if when.weekday() < 5 and start_hour <= when.hour < end_hour:
        return when
    return None


# --- git plumbing --------------------------------------------------------

_REC = "\x1e"
_FLD = "\x1f"


def _git(args):
    return subprocess.run(["git", *args], text=True, capture_output=True)


def collect_commits(base, head):
    fmt = _FLD.join(["%H", "%an", "%ae", "%cn", "%ce", "%ct", "%B"]) + _REC
    if base:
        rng = f"{base}..{head}"
        result = _git(["log", f"--format={fmt}", rng])
    else:
        result = _git(["log", f"--format={fmt}", "-1", head])
    commits = []
    for record in result.stdout.split(_REC):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split(_FLD)
        if len(parts) < 7:
            continue
        commits.append({
            "sha": parts[0], "an": parts[1], "ae": parts[2],
            "cn": parts[3], "ce": parts[4], "ct": int(parts[5] or 0),
            "body": parts[6],
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
    parser = argparse.ArgumentParser(description="Public-history hygiene gate")
    parser.add_argument("--base", default="", help="base ref/sha (merge-base side)")
    parser.add_argument("--head", default="HEAD", help="head ref/sha")
    parser.add_argument("--branch", default="", help="PR head branch name")
    parser.add_argument("--title", default="", help="PR title (squash subject)")
    parser.add_argument("--no-content", action="store_true",
                        help="skip scanning added source/doc lines")
    parser.add_argument("--check-timestamps", action="store_true",
                        help="also fail restricted-window (Mon-Fri 09:00-17:59 ET) commits")
    parser.add_argument("--content-exclude", action="append", default=[],
                        help="extra glob excluded from content scanning")
    parser.add_argument("--bot-email", action="append", default=[],
                        help="extra author email exempt from timestamp checks")
    parser.add_argument("--tz", default=DEFAULT_TZ)
    parser.add_argument("--window-start", type=int, default=DEFAULT_WINDOW_START)
    parser.add_argument("--window-end", type=int, default=DEFAULT_WINDOW_END)
    args = parser.parse_args(argv)

    base = "" if _is_zero(args.base) else args.base
    head = args.head or "HEAD"
    excludes = DEFAULT_CONTENT_EXCLUDES + list(args.content_exclude)
    bot_emails = DEFAULT_BOT_EMAILS | set(args.bot_email)

    violations = []  # (category, detail)

    # 1) commit messages + author/committer identity
    commits = collect_commits(base, head)
    for c in commits:
        short = c["sha"][:12]
        for label in scan_message_ai(c["body"]):
            violations.append(("AI-authorship metadata", f"{short}: {label}"))
        for reason in scan_identity_ai(c["an"], c["ae"]):
            violations.append(("AI-authorship metadata", f"{short} author {reason}"))
        for reason in scan_identity_ai(c["cn"], c["ce"]):
            violations.append(("AI-authorship metadata", f"{short} committer {reason}"))
        if args.check_timestamps:
            when = restricted_window_violation(
                c["ct"], c["ae"], tz=args.tz, start_hour=args.window_start,
                end_hour=args.window_end, bot_emails=bot_emails)
            if when:
                violations.append((
                    "restricted-window timestamp",
                    f"{short}: {when.strftime('%Y-%m-%d %H:%M:%S %Z')}"))

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
    print("Public-history hygiene gate")
    print(f"  scope            : {scope}")
    print(f"  commits scanned  : {len(commits)}")
    print(f"  branch           : {args.branch or '<none>'}")
    print(f"  content scanning : {'off' if args.no_content else 'on'}")
    print(f"  timestamp check  : {'on' if args.check_timestamps else 'off'}")

    if not violations:
        print("OK: no public-history hygiene violations found.")
        return 0

    print(f"FAIL: {len(violations)} hygiene violation(s):")
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
