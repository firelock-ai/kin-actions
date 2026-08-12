"""Workflow-pin PR resolution against a lagging pull request object."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("resolve-workflow-pin-pr.py")
OWNER = "acme"
REPOSITORY = f"{OWNER}/widget"
BRANCH = "automation/kin-actions-pin-next"
PUSHED = "1" * 40
PREVIOUS = "2" * 40


def _load():
    spec = importlib.util.spec_from_file_location("resolve_pin_pr", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


resolver = _load()


def pr_entry(*, number=7, head=PUSHED, owner=OWNER):
    return {
        "number": number,
        "headRefOid": head,
        "headRepositoryOwner": {"login": owner},
    }


class FakeGh:
    """A ``gh`` on PATH that serves one scripted listing per invocation."""

    def __init__(self, responses):
        self._directory = tempfile.TemporaryDirectory()
        root = Path(self._directory.name)
        self.calls = root / "calls"
        self.calls.write_text("", encoding="utf-8")
        payloads = root / "payloads.json"
        payloads.write_text(json.dumps(responses), encoding="utf-8")
        executable = root / "gh"
        executable.write_text(
            textwrap.dedent(
                f"""\
                #!{sys.executable}
                import json, pathlib, sys
                calls = pathlib.Path({str(self.calls)!r})
                payloads = json.loads(
                    pathlib.Path({str(payloads)!r}).read_text(encoding="utf-8")
                )
                seen = [
                    line for line in
                    calls.read_text(encoding="utf-8").splitlines() if line
                ]
                calls.write_text(
                    "".join(f"{{line}}\\n" for line in seen)
                    + " ".join(sys.argv[1:]) + "\\n",
                    encoding="utf-8",
                )
                index = min(len(seen), len(payloads) - 1)
                print(json.dumps(payloads[index]))
                """
            ),
            encoding="utf-8",
        )
        executable.chmod(executable.stat().st_mode | stat.S_IEXEC)
        self.path = str(root)

    def call_count(self):
        return len(
            [
                line
                for line in self.calls.read_text(encoding="utf-8").splitlines()
                if line
            ]
        )

    def cleanup(self):
        self._directory.cleanup()


class ResolveWorkflowPinPr(unittest.TestCase):
    def make_gh(self, responses):
        fake = FakeGh(responses)
        self.addCleanup(fake.cleanup)
        return fake

    def run_script(self, fake, *extra):
        environment = dict(os.environ)
        environment["PATH"] = fake.path + os.pathsep + environment["PATH"]
        return subprocess.run(
            [
                sys.executable, str(SCRIPT),
                "--repository", REPOSITORY,
                "--base", "main",
                "--head-branch", BRANCH,
                "--expect-head", PUSHED,
                "--delay", "0",
                *extra,
            ],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )

    def test_a_single_read_rejects_the_run_that_pushed_the_head(self):
        """The pre-change behaviour: one read, and the lagging oid loses."""

        fake = self.make_gh([[pr_entry(head=PREVIOUS)], [pr_entry()]])
        result = self.run_script(fake, "--attempts", "1")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("is not the exact first-party generated head", result.stderr)
        self.assertEqual(fake.call_count(), 1)

    def test_a_lagging_head_is_re_read_until_github_catches_up(self):
        fake = self.make_gh(
            [[pr_entry(head=PREVIOUS)], [pr_entry(head=PREVIOUS)], [pr_entry()]]
        )
        result = self.run_script(fake)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "7")
        self.assertEqual(fake.call_count(), 3)

    def test_a_pull_request_opened_a_moment_ago_is_waited_for(self):
        fake = self.make_gh([[], [pr_entry(number=11)]])
        result = self.run_script(fake)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "11")

    def test_a_head_that_never_converges_is_still_refused(self):
        fake = self.make_gh([[pr_entry(head=PREVIOUS)]])
        result = self.run_script(fake, "--attempts", "3")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("is not the exact first-party generated head", result.stderr)
        self.assertIn(PREVIOUS, result.stderr)
        self.assertEqual(fake.call_count(), 3)

    def test_a_fork_head_is_refused_on_sight_rather_than_waited_on(self):
        fake = self.make_gh([[pr_entry(owner="attacker")], [pr_entry()]])
        result = self.run_script(fake)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("head repository owner is 'attacker'", result.stderr)
        self.assertEqual(fake.call_count(), 1)

    def test_two_pull_requests_claiming_the_branch_are_refused(self):
        fake = self.make_gh([[pr_entry(number=7), pr_entry(number=8)]])
        result = self.run_script(fake)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("multiple workflow-pin PRs claim", result.stderr)
        self.assertEqual(fake.call_count(), 1)

    def test_an_empty_listing_is_refused_rather_than_read_as_a_pr(self):
        fake = self.make_gh([[]])
        result = self.run_script(fake, "--attempts", "2")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("no open workflow-pin PR claims", result.stderr)

    def test_the_wait_is_bounded_and_paced(self):
        slept = []
        listings = []

        def fake_list(**kwargs):
            listings.append(kwargs)
            return [pr_entry(head=PREVIOUS)]

        original = resolver.list_open_prs
        resolver.list_open_prs = fake_list
        self.addCleanup(setattr, resolver, "list_open_prs", original)
        with self.assertRaises(resolver.ResolveError):
            resolver.resolve_pin_pr(
                repository=REPOSITORY,
                base="main",
                head_branch=BRANCH,
                expect_head=PUSHED,
                attempts=4,
                delay=2.5,
                sleep=slept.append,
            )
        self.assertEqual(len(listings), 4)
        self.assertEqual(slept, [2.5, 2.5, 2.5])

    def test_a_malformed_expected_head_is_refused(self):
        for value in ("", "main", PUSHED.upper(), PUSHED[:-1]):
            with self.subTest(value=value):
                with self.assertRaises(resolver.ResolveError):
                    resolver.resolve_pin_pr(
                        repository=REPOSITORY,
                        base="main",
                        head_branch=BRANCH,
                        expect_head=value,
                        attempts=1,
                        delay=0,
                        sleep=lambda _seconds: None,
                    )


if __name__ == "__main__":
    unittest.main()
