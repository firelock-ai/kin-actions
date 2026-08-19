"""Tests for the scheduled-failure alarm.

The alarm is the precondition for moving any gate onto a schedule, so the thing
that has to be proven is not that it can open an issue. It is that it fires in
both directions and stays quiet in between:

  * a failing scheduled run opens exactly one issue, and a streak updates that
    one issue rather than filing another
  * a scheduled success closes it
  * a run that is neither scheduled nor conclusive writes nothing at all

The last of those is where a consumer like this normally dies. A filter that
can never match writes nothing, which is indistinguishable from a filter that
correctly declined, and the repository looks quiet either way. So every case
asserting "no writes" is paired with a positive control asserting the same
double DOES record writes on the input that should raise the alarm. A check
that cannot fail is not evidence.

The workflow YAML is bound to the script here too, so renaming or rewriting the
step cannot leave this suite green while CI runs something else.
"""

import importlib.util
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "scheduled-failure-alarm.py")
WORKFLOW = os.path.join(
    REPO_ROOT, ".github", "workflows", "scheduled-failure-alarm.yml"
)


def _load():
    spec = importlib.util.spec_from_file_location("scheduled_failure_alarm", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


alarm = _load()


class FakeGitHubApi:
    """An in-memory stand-in for the REST calls the alarm makes.

    Deliberately literal about two behaviours the real API has and that the
    alarm has to survive: the issues listing returns pull requests alongside
    issues, and creating a label that exists fails with `already_exists`.
    """

    def __init__(self, repository="firelock-ai/example"):
        self.repository = repository
        self.issues = []
        self.labels = set()
        self.writes = []
        self.comments = []
        self._next_number = 1

    # -- helpers the tests drive directly ------------------------------------

    def add_raw_issue(self, **fields):
        issue = {
            "number": self._next_number,
            "state": "open",
            "body": "",
            "labels": [],
        }
        issue.update(fields)
        self._next_number = max(self._next_number, issue["number"]) + 1
        self.issues.append(issue)
        return issue

    def open_issues(self):
        return [i for i in self.issues if i.get("state") == "open"]

    def issue(self, number):
        for candidate in self.issues:
            if candidate["number"] == number:
                return candidate
        raise AssertionError(f"no issue #{number}")

    # -- the gh_api-compatible entry point -----------------------------------

    def __call__(self, args, *, method="GET", fields=None):
        fields = dict(fields or {})
        path = args[0]
        extra = [args[i + 1] for i, a in enumerate(args) if a == "-f"]
        for pair in extra:
            name, _, value = pair.partition("=")
            fields.setdefault(name, value)

        if method == "GET" and "/issues?" in path:
            label = re.search(r"[?&]labels=([^&]*)", path).group(1)
            # `[?&]` matters: `page=(\d+)` alone matches inside `per_page=100`
            # and every listing then reads as an empty page 100, which makes
            # the alarm file a fresh issue every time. That is the duplicate
            # storm these tests exist to catch, arriving from the double.
            page = int(re.search(r"[?&]page=(\d+)", path).group(1))
            matched = [i for i in self.issues if label in i.get("labels", [])]
            start = (page - 1) * alarm.PAGE_SIZE
            return matched[start : start + alarm.PAGE_SIZE]

        if method == "POST" and path.endswith("/labels"):
            self.writes.append(("label", fields.get("name")))
            if fields.get("name") in self.labels:
                raise alarm.AlarmError("HTTP 422: already_exists")
            self.labels.add(fields.get("name"))
            return {"name": fields.get("name")}

        if method == "POST" and path.endswith("/issues"):
            labels = [v for k, v in fields.items() if k == "labels[]"]
            labels += [
                p.partition("=")[2] for p in extra if p.startswith("labels[]=")
            ]
            issue = self.add_raw_issue(
                title=fields.get("title", ""),
                body=fields.get("body", ""),
                labels=sorted(set(labels)),
            )
            self.writes.append(("create", issue["number"]))
            return issue

        if method == "POST" and path.endswith("/comments"):
            number = int(path.rsplit("/", 2)[-2])
            self.comments.append((number, fields.get("body", "")))
            self.writes.append(("comment", number))
            return {"id": 1}

        if method == "PATCH" and "/issues/" in path:
            number = int(path.rsplit("/", 1)[-1])
            issue = self.issue(number)
            issue.update({k: v for k, v in fields.items() if k != "state_reason"})
            self.writes.append(("patch", number))
            return issue

        raise AssertionError(f"unexpected call: {method} {path} {fields}")


def drive(fake, *, conclusion, event="schedule", run_id="1", name="Nightly Windows",
          path=".github/workflows/nightly-windows.yml", label="scheduled-failure",
          alarm_events="schedule", run_url="https://example.invalid/run/1"):
    """Run the shipped entry point once against the double.

    Arguments go through the shipped parser rather than a hand-built namespace,
    so a flag renamed in the script without the workflow following it shows up
    here.
    """

    args = alarm.build_parser().parse_args(
        [
            "--repository", fake.repository,
            "--workflow-name", name,
            "--workflow-path", path,
            "--conclusion", conclusion,
            "--event", event,
            "--run-url", run_url,
            "--run-id", run_id,
            "--label", label,
            "--alarm-events", alarm_events,
        ]
    )
    return alarm.run(args, alarm.GitHub(fake.repository, api=fake))


class RaisesOnScheduledFailureTest(unittest.TestCase):
    """The positive control. Everything asserting silence depends on this."""

    def test_one_failure_opens_one_issue(self):
        fake = FakeGitHubApi()
        self.assertEqual(drive(fake, conclusion="failure"), 0)
        self.assertEqual(len(fake.open_issues()), 1)
        issue = fake.open_issues()[0]
        self.assertIn("Nightly Windows", issue["title"])
        self.assertIn("1 consecutive run", issue["title"])
        self.assertIn("scheduled-failure", issue["labels"])

    def test_issue_names_the_workflow_the_run_and_the_count(self):
        fake = FakeGitHubApi()
        drive(fake, conclusion="failure", run_url="https://example.invalid/run/77")
        body = fake.open_issues()[0]["body"]
        self.assertIn("Nightly Windows", body)
        self.assertIn(".github/workflows/nightly-windows.yml", body)
        self.assertIn("https://example.invalid/run/77", body)
        self.assertIn("| Consecutive failed scheduled runs | **1** |", body)

    def test_timed_out_and_startup_failure_also_raise(self):
        for conclusion in ("timed_out", "startup_failure"):
            with self.subTest(conclusion=conclusion):
                fake = FakeGitHubApi()
                drive(fake, conclusion=conclusion)
                self.assertEqual(len(fake.open_issues()), 1)


class ConsecutiveCountTest(unittest.TestCase):
    def test_a_streak_updates_one_issue_and_never_files_another(self):
        fake = FakeGitHubApi()
        for run_id in ("1", "2", "3", "4", "5"):
            drive(fake, conclusion="failure", run_id=run_id)
        self.assertEqual(len(fake.issues), 1, "the alarm filed more than one issue")
        issue = fake.issues[0]
        self.assertIn("(5 consecutive runs)", issue["title"])
        self.assertIn("| Consecutive failed scheduled runs | **5** |", issue["body"])
        self.assertEqual(
            [w for w in fake.writes if w[0] == "create"],
            [("create", 1)],
            "a streak must create exactly one issue",
        )
        self.assertEqual(fake.comments, [], "a streak must not comment per failure")

    def test_the_same_run_delivered_twice_does_not_increment(self):
        fake = FakeGitHubApi()
        drive(fake, conclusion="failure", run_id="42")
        drive(fake, conclusion="failure", run_id="42")
        drive(fake, conclusion="failure", run_id="42")
        self.assertIn("(1 consecutive run)", fake.issues[0]["title"])

    def test_count_restarts_after_a_recovery(self):
        fake = FakeGitHubApi()
        drive(fake, conclusion="failure", run_id="1")
        drive(fake, conclusion="failure", run_id="2")
        drive(fake, conclusion="success", run_id="3")
        drive(fake, conclusion="failure", run_id="4")
        self.assertIn("(1 consecutive run)", fake.issue(1)["title"])
        self.assertEqual(fake.issue(1)["state"], "open")
        self.assertEqual(len(fake.issues), 1, "recovery then failure must reuse the issue")

    def test_two_workflows_keep_independent_counts_and_issues(self):
        fake = FakeGitHubApi()
        drive(fake, conclusion="failure", run_id="1", name="A",
              path=".github/workflows/a.yml")
        drive(fake, conclusion="failure", run_id="2", name="A",
              path=".github/workflows/a.yml")
        drive(fake, conclusion="failure", run_id="3", name="B",
              path=".github/workflows/b.yml")
        self.assertEqual(len(fake.issues), 2)
        titles = sorted(i["title"] for i in fake.issues)
        self.assertIn("(2 consecutive runs)", titles[0])
        self.assertIn("(1 consecutive run)", titles[1])


class ClearsOnScheduledSuccessTest(unittest.TestCase):
    def test_success_closes_the_open_alarm(self):
        fake = FakeGitHubApi()
        drive(fake, conclusion="failure", run_id="1")
        self.assertEqual(fake.issue(1)["state"], "open")
        drive(fake, conclusion="success", run_id="2")
        self.assertEqual(fake.issue(1)["state"], "closed")

    def test_success_comments_once_with_the_clearing_run(self):
        fake = FakeGitHubApi()
        drive(fake, conclusion="failure", run_id="1")
        drive(fake, conclusion="failure", run_id="2")
        drive(fake, conclusion="success", run_id="3",
              run_url="https://example.invalid/run/green")
        self.assertEqual(len(fake.comments), 1)
        number, body = fake.comments[0]
        self.assertEqual(number, 1)
        self.assertIn("https://example.invalid/run/green", body)
        self.assertIn("2 consecutive failed runs", body)

    def test_success_with_no_open_alarm_writes_nothing(self):
        fake = FakeGitHubApi()
        self.assertEqual(drive(fake, conclusion="success"), 0)
        self.assertEqual(fake.writes, [])
        # Positive control on the same double: it does record writes when the
        # input should raise the alarm, so the empty list above is a decision
        # rather than a broken harness.
        drive(fake, conclusion="failure")
        self.assertTrue(fake.writes)

    def test_a_cleared_alarm_records_the_streak_as_ended(self):
        # The zero is what tells the next failure to start a new count. A
        # closed issue that still claimed a streak would make the next failure
        # resume it, and the alarm would report a count nobody could reconcile
        # against the runs.
        fake = FakeGitHubApi()
        drive(fake, conclusion="failure", run_id="1")
        drive(fake, conclusion="failure", run_id="2")
        drive(fake, conclusion="success", run_id="3")
        state = alarm.parse_marker(fake.issue(1)["body"])
        self.assertEqual(state["consecutive"], 0)
        self.assertIn("Streak that ended | 2", fake.issue(1)["body"])

    def test_second_success_does_not_reopen_or_recomment(self):
        fake = FakeGitHubApi()
        drive(fake, conclusion="failure", run_id="1")
        drive(fake, conclusion="success", run_id="2")
        before = list(fake.writes)
        drive(fake, conclusion="success", run_id="3")
        self.assertEqual(fake.writes, before)


class ReopensRatherThanDuplicatesTest(unittest.TestCase):
    """Closing by hand while the schedule is still broken must not duplicate."""

    def test_failure_after_a_manual_close_reopens_the_same_issue(self):
        fake = FakeGitHubApi()
        drive(fake, conclusion="failure", run_id="1")
        drive(fake, conclusion="failure", run_id="2")
        fake.issue(1)["state"] = "closed"
        drive(fake, conclusion="failure", run_id="3")
        self.assertEqual(len(fake.issues), 1, "a manual close must not spawn a duplicate")
        self.assertEqual(fake.issue(1)["state"], "open")
        self.assertIn("(3 consecutive runs)", fake.issue(1)["title"])


class StaysQuietTest(unittest.TestCase):
    """Every silence here is paired with a control proving silence was chosen."""

    def _assert_silent_then_prove_the_double_writes(self, fake, **kwargs):
        self.assertEqual(drive(fake, **kwargs), 0)
        self.assertEqual(fake.writes, [], f"expected no writes for {kwargs}")
        self.assertEqual(fake.issues, [])
        drive(fake, conclusion="failure", event="schedule", run_id="control")
        self.assertTrue(fake.writes, "the double never writes, so the silence proves nothing")

    def test_a_pull_request_run_is_ignored(self):
        self._assert_silent_then_prove_the_double_writes(
            FakeGitHubApi(), conclusion="failure", event="pull_request"
        )

    def test_a_push_run_is_ignored(self):
        self._assert_silent_then_prove_the_double_writes(
            FakeGitHubApi(), conclusion="failure", event="push"
        )

    def test_a_manual_dispatch_is_ignored_by_default(self):
        self._assert_silent_then_prove_the_double_writes(
            FakeGitHubApi(), conclusion="failure", event="workflow_dispatch"
        )

    def test_a_cancelled_scheduled_run_neither_raises_nor_clears(self):
        self._assert_silent_then_prove_the_double_writes(
            FakeGitHubApi(), conclusion="cancelled"
        )

    def test_a_skipped_scheduled_run_does_not_clear_the_alarm(self):
        # A skipped run is the failure mode this fleet already lived through:
        # a job that skipped 203 times read as green. It must not close an
        # open alarm.
        fake = FakeGitHubApi()
        drive(fake, conclusion="failure", run_id="1")
        drive(fake, conclusion="skipped", run_id="2")
        self.assertEqual(fake.issue(1)["state"], "open")

    def test_dispatch_is_covered_when_the_caller_asks_for_it(self):
        fake = FakeGitHubApi()
        drive(fake, conclusion="failure", event="workflow_dispatch",
              alarm_events="schedule,workflow_dispatch")
        self.assertEqual(len(fake.open_issues()), 1)


class IssueDiscriminationTest(unittest.TestCase):
    def test_a_pull_request_carrying_the_marker_is_not_mistaken_for_the_alarm(self):
        # The issues REST listing returns pull requests too. Patching one
        # instead of the alarm issue would rewrite somebody's PR body.
        fake = FakeGitHubApi()
        fake.add_raw_issue(
            title="A PR that quotes the marker",
            body=alarm.render_marker("a-key", 9, "999"),
            labels=["scheduled-failure"],
            pull_request={"url": "https://example.invalid/pulls/1"},
        )
        drive(fake, conclusion="failure", run_id="1", path="a-key")
        self.assertEqual(len(fake.issues), 2, "the alarm must open its own issue")
        self.assertIn("(1 consecutive run)", fake.issue(2)["title"])

    def test_another_workflows_alarm_is_not_adopted(self):
        fake = FakeGitHubApi()
        drive(fake, conclusion="failure", run_id="1", name="A",
              path=".github/workflows/a.yml")
        drive(fake, conclusion="failure", run_id="2", name="B",
              path=".github/workflows/b.yml")
        self.assertEqual(len(fake.issues), 2)

    def test_an_unlabelled_issue_is_invisible_to_the_alarm(self):
        fake = FakeGitHubApi()
        fake.add_raw_issue(
            title="unlabelled",
            body=alarm.render_marker("a-key", 4, "5"),
            labels=[],
        )
        drive(fake, conclusion="failure", run_id="1", path="a-key")
        self.assertEqual(len(fake.issues), 2)


class MarkerTest(unittest.TestCase):
    def test_round_trip(self):
        marker = alarm.render_marker(".github/workflows/x.yml", 7, "123")
        state = alarm.parse_marker(f"prose\n{marker}\nmore prose")
        self.assertEqual(
            state, {"key": ".github/workflows/x.yml", "consecutive": 7, "run": "123"}
        )

    def test_a_body_without_a_marker_parses_as_none(self):
        self.assertIsNone(alarm.parse_marker("no marker here"))
        self.assertIsNone(alarm.parse_marker(""))

    def test_key_sanitising_can_never_break_the_html_comment(self):
        # `--` terminates an HTML comment. A key that produced one would make
        # the marker unparseable, and every later failure would file a fresh
        # issue: the duplicate storm this design exists to prevent.
        for raw in (
            "a -- b",
            "weird  name!! here",
            "  spaces  ",
            "emoji \U0001f600 name",
            "a---------b",
        ):
            with self.subTest(raw=raw):
                key = alarm.sanitize_key(raw)
                self.assertNotIn("--", key)
                marker = alarm.render_marker(key, 1, "1")
                self.assertEqual(alarm.parse_marker(marker)["key"], key)

    def test_an_unusable_key_is_refused_rather_than_guessed(self):
        with self.assertRaises(alarm.AlarmError):
            alarm.sanitize_key("!!!")

    def test_path_is_preferred_over_name_so_a_rename_keeps_the_alarm(self):
        fake = FakeGitHubApi()
        drive(fake, conclusion="failure", run_id="1", name="Old Name",
              path=".github/workflows/nightly.yml")
        drive(fake, conclusion="failure", run_id="2", name="Brand New Name",
              path=".github/workflows/nightly.yml")
        self.assertEqual(len(fake.issues), 1)
        self.assertIn("Brand New Name", fake.issue(1)["title"])
        self.assertIn("(2 consecutive runs)", fake.issue(1)["title"])


class FailsLoudTest(unittest.TestCase):
    """A silent alarm is the defect. An API refusal must go red."""

    def test_an_api_failure_is_not_swallowed(self):
        class Broken(FakeGitHubApi):
            def __call__(self, args, *, method="GET", fields=None):
                raise alarm.AlarmError("HTTP 403: forbidden")

        with self.assertRaises(alarm.AlarmError):
            drive(Broken(), conclusion="failure")

    def test_main_reports_an_api_failure_as_a_nonzero_exit(self):
        argv = [
            "--repository", "firelock-ai/example",
            "--workflow-name", "x",
            "--conclusion", "failure",
            "--event", "schedule",
        ]
        original = alarm.gh_api

        def broken(args, *, method="GET", fields=None):
            raise alarm.AlarmError("HTTP 403: forbidden")

        alarm.gh_api = broken
        try:
            # GitHub captures its api callable at construction from the module
            # default, so patching the module attribute is what the entry point
            # actually reads.
            self.assertEqual(alarm.main(argv), 1)
        finally:
            alarm.gh_api = original

    def test_a_malformed_repository_is_refused(self):
        self.assertEqual(
            alarm.main(
                ["--repository", "not-a-repo", "--workflow-name", "x",
                 "--conclusion", "failure", "--event", "schedule"]
            ),
            1,
        )

    def test_label_creation_tolerates_an_existing_label_only(self):
        fake = FakeGitHubApi()
        drive(fake, conclusion="failure", run_id="1")
        fake.issues.clear()
        # Second create hits an existing label; the alarm must carry on.
        drive(fake, conclusion="failure", run_id="2")
        self.assertEqual(len(fake.issues), 1)

        class HardLabelFailure(FakeGitHubApi):
            def __call__(self, args, *, method="GET", fields=None):
                if method == "POST" and args[0].endswith("/labels"):
                    raise alarm.AlarmError("HTTP 403: forbidden")
                return super().__call__(args, method=method, fields=fields)

        with self.assertRaises(alarm.AlarmError):
            drive(HardLabelFailure(), conclusion="failure")


class DryRunTest(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        fake = FakeGitHubApi()
        args = alarm.build_parser().parse_args(
            ["--repository", fake.repository, "--workflow-name", "x",
             "--conclusion", "failure", "--event", "schedule", "--dry-run"]
        )
        alarm.run(args, alarm.GitHub(fake.repository, api=fake))
        self.assertEqual(fake.writes, [])


class WorkflowBindingTest(unittest.TestCase):
    """The shipped YAML has to invoke the script these tests exercised."""

    @classmethod
    def setUpClass(cls):
        with open(WORKFLOW, encoding="utf-8") as handle:
            cls.text = handle.read()

    def test_the_workflow_invokes_this_script(self):
        self.assertIn("python3 scripts/scheduled-failure-alarm.py", self.text)
        self.assertTrue(os.path.isfile(SCRIPT))

    def test_every_flag_the_workflow_passes_is_a_real_flag(self):
        flags = set(re.findall(r"^\s*--([a-z-]+) ", self.text, re.MULTILINE))
        parser_flags = {
            action.option_strings[0].lstrip("-")
            for action in alarm.build_parser()._actions
            if action.option_strings
        }
        self.assertTrue(flags, "no flags found in the workflow; the check is vacuous")
        self.assertEqual(flags - parser_flags, set())

    def test_event_values_are_read_through_env_not_interpolated(self):
        # A workflow name is repository-controlled text. Interpolating it into
        # the shell would make the alarm an injection surface.
        self.assertIn("WORKFLOW_NAME: ${{ github.event.workflow_run.name }}", self.text)
        run_block = self.text.split("run: |", 1)[1]
        self.assertNotIn("${{", run_block)

    def test_it_is_a_reusable_workflow_with_issue_write(self):
        self.assertIn("workflow_call:", self.text)
        self.assertIn("issues: write", self.text)

    def test_the_helper_is_bound_to_the_called_workflow_source(self):
        self.assertIn("repository: ${{ job.workflow_repository }}", self.text)
        self.assertIn("ref: ${{ job.workflow_sha }}", self.text)

    def test_it_reaches_for_no_workflow_run_history(self):
        # The count comes from the issue, never from a runs list. A runs-list
        # lookup decays with run retention and this repository's guard refuses
        # it, so the alarm would break silently once history aged out.
        with open(SCRIPT, encoding="utf-8") as handle:
            script = handle.read()
        for text in (self.text, script):
            self.assertNotIn("actions/runs/", text)
            self.assertNotIn("actions/workflows/", text)


if __name__ == "__main__":
    unittest.main()
