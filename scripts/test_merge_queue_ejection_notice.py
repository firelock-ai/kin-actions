"""Tests for the merge-queue ejection notice.

The notice exists because a PR dropped from the hosted merge queue leaves no
trace on the PR itself: the failing run is attributed to a gh-readonly-queue ref
and names no pull request, while PR-level state keeps reading clean. The only
thing tying that run back to a PR is the number embedded in the queue ref, so
the parser that reads it is the whole mechanism, and a parser that quietly
declines to match is the original defect wearing a different hat.

These tests run the SHIPPED shell out of the workflow file rather than a copy of
it, so a rewrite of the step cannot leave the suite green while changing what
actually runs in CI.
"""
import os
import re
import subprocess
import textwrap
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(
    REPO_ROOT, ".github", "workflows", "merge-queue-ejection-notice.yml"
)

# Everything from `set -euo pipefail` down to this marker is pure ref parsing
# with no network access, which is the part under test.
_NETWORK_MARKER = 'echo "attributing failed merge_group run'


def _workflow_text():
    with open(WORKFLOW, encoding="utf-8") as handle:
        return handle.read()


def _extract_parser():
    """Return the ref-parsing prologue of the workflow's run: block."""
    text = _workflow_text()
    match = re.search(r"^( +)run: \|\s*$", text, re.MULTILINE)
    if not match:
        raise AssertionError("no 'run: |' block found in %s" % WORKFLOW)
    indent = match.group(1) + "  "
    body = []
    for line in text[match.end():].splitlines()[1:]:
        if line.strip() and not line.startswith(indent):
            break
        body.append(line)
    script = textwrap.dedent("\n".join(body))
    head, sep, _ = script.partition(_NETWORK_MARKER)
    if not sep:
        raise AssertionError(
            "run: block no longer contains %r, so the no-network prologue "
            "could not be isolated; update this test with the step"
            % _NETWORK_MARKER
        )
    return head


def _run(head_branch):
    """Run the shipped parser against one ref. Returns (rc, pr_or_marker)."""
    script = _extract_parser() + '\nprintf "PR=%s\\n" "$pr"\n'
    proc = subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "HEAD_BRANCH": head_branch},
        text=True,
        capture_output=True,
    )
    out = proc.stdout.strip()
    if proc.returncode != 0:
        return proc.returncode, "ERROR"
    if "not a merge-queue ref" in out:
        return 0, "SKIP"
    found = re.search(r"^PR=(\d+)$", out, re.MULTILINE)
    return 0, found.group(1) if found else out


class ExtractionIsRealTest(unittest.TestCase):
    """A vacuous extraction would make every case below pass for free."""

    def test_prologue_contains_the_parse(self):
        head = _extract_parser()
        self.assertIn("gh-readonly-queue", head)
        self.assertIn('pr="${pr%%-*}"', head)

    def test_step_is_gated_to_failed_merge_group_runs(self):
        text = _workflow_text()
        self.assertIn("github.event.workflow_run.event == 'merge_group'", text)
        self.assertIn("github.event.workflow_run.conclusion == 'failure'", text)

    def test_event_values_are_read_through_env_not_interpolated(self):
        # A queue ref is attacker-influenceable through a branch name, so it
        # must never reach the shell as an expression substitution.
        text = _workflow_text()
        self.assertNotIn("${{ github.event.workflow_run.head_branch }}\n          ref=", text)
        self.assertIn("HEAD_BRANCH: ${{ github.event.workflow_run.head_branch }}", text)


class RealEjectionRefTest(unittest.TestCase):
    """The refs below were taken from observed merge-queue runs."""

    def test_observed_ejection_ref_resolves_to_its_pr(self):
        rc, pr = _run(
            "gh-readonly-queue/main/pr-26-"
            "5849739c6ba743b08ed6c85414c8cf8cb6319fa5"
        )
        self.assertEqual((rc, pr), (0, "26"))

    def test_second_observed_queue_ref(self):
        rc, pr = _run(
            "gh-readonly-queue/main/pr-24-"
            "d2d03399307cb39b6cd34df1c9f0e672d63177d8"
        )
        self.assertEqual((rc, pr), (0, "24"))

    def test_multi_digit_pr(self):
        self.assertEqual(_run("gh-readonly-queue/main/pr-1234-abc"), (0, "1234"))

    def test_base_branch_containing_slashes_and_dashes(self):
        self.assertEqual(
            _run("gh-readonly-queue/release/v2-x/pr-7-deadbeef"), (0, "7")
        )

    def test_fully_qualified_ref_is_still_recognised(self):
        # head_branch arrives unprefixed today. If that ever changes, the
        # notice must keep firing rather than go quiet.
        self.assertEqual(
            _run("refs/heads/gh-readonly-queue/main/pr-9-aa"), (0, "9")
        )


class NonQueueRefTest(unittest.TestCase):
    """A non-queue ref must be skipped, never attributed to some PR."""

    def test_default_branch(self):
        self.assertEqual(_run("main"), (0, "SKIP"))

    def test_ordinary_topic_branch(self):
        self.assertEqual(_run("chore/lane-example"), (0, "SKIP"))


class MalformedQueueRefTest(unittest.TestCase):
    """A queue ref this cannot parse must fail loud, not guess a number."""

    def test_missing_number(self):
        self.assertEqual(_run("gh-readonly-queue/main/pr-"), (1, "ERROR"))

    def test_non_numeric_number(self):
        self.assertEqual(_run("gh-readonly-queue/main/pr-abc-def"), (1, "ERROR"))

    def test_empty_number_field(self):
        self.assertEqual(_run("gh-readonly-queue/main/pr--1-aa"), (1, "ERROR"))


if __name__ == "__main__":
    unittest.main()
