"""Adversarial tests for exact-head generated release-PR check recovery."""

from __future__ import annotations

import importlib.util
import unittest
from contextlib import ExitStack
from unittest.mock import patch
from pathlib import Path


SCRIPT = Path(__file__).with_name("recover-release-pr-checks.py")
SPEC = importlib.util.spec_from_file_location("recover_release_pr_checks", SCRIPT)
assert SPEC and SPEC.loader
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)

HEAD = "a" * 40
MAIN = "b" * 40
MERGE = "c" * 40
TREE = "d" * 40
OTHER_TREE = "e" * 40
CONTEXTS = (
    "release / Version bump gate",
    "release / Registry-only build",
    "release / Repo verification",
)


class ReleasePrCheckRecoveryTests(unittest.TestCase):
    def branch(self, *, first_app_id: int | None = 15368) -> dict[str, object]:
        checks = [
            {"context": context, "app_id": 15368}
            for context in CONTEXTS
        ]
        checks[0]["app_id"] = first_app_id
        return {
            "strict": True,
            "checks": checks,
        }

    def pr(self) -> dict[str, object]:
        return {
            "autoMergeRequest": {"enabledAt": "2026-07-28T00:00:00Z"},
            "baseRefName": "main",
            "headRefOid": HEAD,
            "headRepositoryOwner": {"login": "firelock-ai"},
            "mergeCommit": None,
            "mergedAt": None,
            "state": "OPEN",
            "url": "https://github.com/firelock-ai/demo/pull/7",
        }

    def check_page(
        self,
        *,
        conclusion: str,
        status: str = "completed",
        app_id: int = 15368,
        run_id: int = 123,
    ) -> list[dict[str, object]]:
        return [
            {
                "check_runs": [
                    {
                        "id": index + 1,
                        "name": context,
                        "status": status,
                        "conclusion": conclusion,
                        "details_url": (
                            "https://github.com/firelock-ai/demo/actions/"
                            f"runs/{run_id}/job/{index + 10}"
                        ),
                        "app": {"id": app_id},
                    }
                    for index, context in enumerate(CONTEXTS)
                ]
            }
        ]

    def run_metadata(
        self,
        *,
        attempt: int = 1,
        head: str = HEAD,
        workflow: str = "Registry Publish",
        conclusion: str = "failure",
        status: str = "completed",
    ) -> dict[str, object]:
        return {
            "event": "pull_request",
            "conclusion": conclusion,
            "head_repository": {"full_name": "firelock-ai/demo"},
            "head_sha": head,
            "name": workflow,
            "run_attempt": attempt,
            "status": status,
        }

    def execute(
        self,
        check_pages: list[list[dict[str, object]]],
        *,
        main: str = MAIN,
        branch: dict[str, object] | None = None,
        run: dict[str, object] | None = None,
        timeout: int = 1,
        merge_on_pr_call: int | None = None,
        monotonic_values: list[float] | None = None,
        merge_pr_commit: object = MERGE,
        head_commit: dict[str, object] | None = None,
        merge_commit: dict[str, object] | None = None,
    ):
        pages = iter(check_pages)
        reruns: list[tuple[str, ...]] = []
        pr_calls = 0

        def fake_json(args, token):
            nonlocal pr_calls
            joined = " ".join(args)
            if "branches/main/protection/required_status_checks" in joined:
                self.assertEqual(token, "protection")
            else:
                self.assertEqual(token, "actions")
            if args[:2] == ["pr", "view"]:
                pr_calls += 1
                value = self.pr()
                if (
                    merge_on_pr_call is not None
                    and pr_calls >= merge_on_pr_call
                ):
                    value["mergedAt"] = "2026-07-28T00:01:00Z"
                    value["state"] = "MERGED"
                    value["mergeCommit"] = (
                        {"oid": merge_pr_commit}
                        if isinstance(merge_pr_commit, str)
                        else merge_pr_commit
                    )
                return value
            if f"commits/main" in joined:
                return {"sha": main}
            if "branches/main/protection/required_status_checks" in joined:
                return branch or self.branch()
            if "check-runs" in joined:
                return next(pages)
            if f"commits/{HEAD}" in joined:
                return head_commit or {
                    "sha": HEAD,
                    "commit": {"tree": {"sha": TREE}},
                }
            if f"commits/{MERGE}" in joined:
                return merge_commit or {
                    "sha": MERGE,
                    "parents": [{"sha": MAIN}],
                    "commit": {"tree": {"sha": TREE}},
                }
            if "actions/runs/123" in joined:
                return run or self.run_metadata()
            raise AssertionError(f"unexpected gh JSON call: {args}")

        def fake_no_content(args, _token):
            reruns.append(tuple(args))

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(recovery, "gh_json", side_effect=fake_json)
            )
            stack.enter_context(
                patch.object(
                    recovery,
                    "gh_no_content",
                    side_effect=fake_no_content,
                )
            )
            stack.enter_context(
                patch.object(recovery.time, "sleep", return_value=None)
            )
            if monotonic_values is not None:
                stack.enter_context(
                    patch.object(
                        recovery.time,
                        "monotonic",
                        side_effect=iter(monotonic_values),
                    )
                )
            state = recovery.recover(
                repository="firelock-ai/demo",
                pull_request=7,
                expected_head=HEAD,
                trusted_main=MAIN,
                default_branch="main",
                release_checks=CONTEXTS,
                actions_app_id=15368,
                actions_token="actions",
                protection_token="protection",
                timeout_seconds=timeout,
                poll_seconds=0,
                max_attempts=3,
            )
        return state, reruns

    def test_failed_exact_run_is_rerun_then_checks_turn_green(self) -> None:
        state, reruns = self.execute(
            [
                self.check_page(conclusion="failure"),
                self.check_page(conclusion="success"),
            ],
            merge_on_pr_call=3,
        )
        self.assertEqual(state, "merged")
        self.assertEqual(len(reruns), 1)
        self.assertIn("rerun-failed-jobs", reruns[0][-1])

    def test_pending_checks_wait_without_duplicate_rerun(self) -> None:
        state, reruns = self.execute(
            [
                self.check_page(conclusion="", status="in_progress"),
                self.check_page(conclusion="success"),
            ],
            merge_on_pr_call=3,
        )
        self.assertEqual(state, "merged")
        self.assertEqual(reruns, [])

    def test_cancelled_exact_run_uses_bounded_full_rerun(self) -> None:
        state, reruns = self.execute(
            [
                self.check_page(conclusion="cancelled"),
                self.check_page(conclusion="success"),
            ],
            run=self.run_metadata(conclusion="cancelled"),
            merge_on_pr_call=3,
        )
        self.assertEqual(state, "merged")
        self.assertEqual(len(reruns), 1)
        self.assertTrue(reruns[0][-1].endswith("/rerun"))

    def test_action_required_run_has_no_automatic_bypass(self) -> None:
        with self.assertRaisesRegex(
            recovery.RecoveryError,
            "has no safe automatic rerun",
        ):
            self.execute(
                [self.check_page(conclusion="action_required")],
                run=self.run_metadata(conclusion="action_required"),
            )

    def test_running_exact_workflow_waits_before_rerun(self) -> None:
        state, reruns = self.execute(
            [
                self.check_page(conclusion="failure"),
                self.check_page(conclusion="success"),
            ],
            run=self.run_metadata(status="in_progress", conclusion=""),
            merge_on_pr_call=3,
        )
        self.assertEqual(state, "merged")
        self.assertEqual(reruns, [])

    def test_green_checks_without_auto_merge_are_terminal(self) -> None:
        with self.assertRaisesRegex(
            recovery.RecoveryError,
            "checks became green but exact-head auto-merge did not complete",
        ):
            self.execute(
                [self.check_page(conclusion="success")],
                monotonic_values=[0.0, 0.0, 2.0],
            )

    def test_stale_main_never_reruns_stale_head(self) -> None:
        with self.assertRaisesRegex(
            recovery.RecoveryError,
            "stale-head auto-merge must be disarmed",
        ):
            self.execute(
                [self.check_page(conclusion="failure")],
                main="c" * 40,
            )

    def test_merge_race_accepts_only_exact_trusted_parent_and_tree(self) -> None:
        state, reruns = self.execute(
            [self.check_page(conclusion="success")],
            merge_on_pr_call=2,
        )
        self.assertEqual(state, "merged")
        self.assertEqual(reruns, [])

    def test_merge_race_rejects_commit_based_on_advanced_main(self) -> None:
        with self.assertRaisesRegex(
            recovery.RecoveryError,
            "parent is not the trusted main",
        ):
            self.execute(
                [self.check_page(conclusion="success")],
                merge_on_pr_call=2,
                merge_commit={
                    "sha": MERGE,
                    "parents": [{"sha": "f" * 40}],
                    "commit": {"tree": {"sha": TREE}},
                },
            )

    def test_merged_pr_without_merge_commit_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            recovery.RecoveryError,
            "merge commit must be one JSON object",
        ):
            self.execute(
                [self.check_page(conclusion="success")],
                merge_on_pr_call=2,
                merge_pr_commit=None,
            )

    def test_malformed_merge_commit_oid_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            recovery.RecoveryError,
            "40-hex oid",
        ):
            self.execute(
                [self.check_page(conclusion="success")],
                merge_on_pr_call=2,
                merge_pr_commit="not-a-sha",
            )

    def test_missing_or_malformed_merge_objects_fail_closed(self) -> None:
        cases = (
            (
                "head-tree",
                {"sha": HEAD, "commit": {}},
                None,
                "tree must be one JSON object",
            ),
            (
                "parents",
                None,
                {
                    "sha": MERGE,
                    "commit": {"tree": {"sha": TREE}},
                },
                "parents must be one JSON list",
            ),
            (
                "multiple-parents",
                None,
                {
                    "sha": MERGE,
                    "parents": [{"sha": MAIN}, {"sha": "f" * 40}],
                    "commit": {"tree": {"sha": TREE}},
                },
                "must have exactly one parent",
            ),
            (
                "merge-tree",
                None,
                {
                    "sha": MERGE,
                    "parents": [{"sha": MAIN}],
                    "commit": {"tree": {}},
                },
                "tree must have one lowercase 40-hex SHA",
            ),
        )
        for name, head_commit, merge_commit, message in cases:
            with self.subTest(name=name):
                with self.assertRaisesRegex(recovery.RecoveryError, message):
                    self.execute(
                        [self.check_page(conclusion="success")],
                        merge_on_pr_call=2,
                        head_commit=head_commit,
                        merge_commit=merge_commit,
                    )

    def test_merge_tree_must_equal_expected_generated_head_tree(self) -> None:
        with self.assertRaisesRegex(
            recovery.RecoveryError,
            "does not equal the expected generated head tree",
        ):
            self.execute(
                [self.check_page(conclusion="success")],
                merge_on_pr_call=2,
                merge_commit={
                    "sha": MERGE,
                    "parents": [{"sha": MAIN}],
                    "commit": {"tree": {"sha": OTHER_TREE}},
                },
            )

    def test_attempt_exhaustion_is_terminal(self) -> None:
        with self.assertRaisesRegex(
            recovery.RecoveryError,
            "exhausted bounded attempt",
        ):
            self.execute(
                [self.check_page(conclusion="failure")],
                run=self.run_metadata(attempt=3),
            )

    def test_wrong_head_run_is_never_rerun(self) -> None:
        with self.assertRaisesRegex(
            recovery.RecoveryError,
            "unexpected Actions run",
        ):
            self.execute(
                [self.check_page(conclusion="failure")],
                run=self.run_metadata(head="d" * 40),
            )

    def test_unbound_release_check_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            recovery.RecoveryError,
            "not uniquely bound",
        ):
            self.execute(
                [self.check_page(conclusion="success")],
                branch=self.branch(first_app_id=None),
            )

    def test_non_strict_required_checks_are_rejected(self) -> None:
        branch = self.branch()
        branch["strict"] = False
        with self.assertRaisesRegex(
            recovery.RecoveryError,
            "strict/up-to-date",
        ):
            self.execute(
                [self.check_page(conclusion="success")],
                branch=branch,
            )

    def test_required_check_run_name_does_not_create_staleness(self) -> None:
        state, reruns = self.execute(
            [
                self.check_page(conclusion="failure"),
                self.check_page(conclusion="success"),
            ],
            run=self.run_metadata(workflow="Acceleration Matrix"),
            merge_on_pr_call=3,
        )
        self.assertEqual(state, "merged")
        self.assertEqual(len(reruns), 1)


if __name__ == "__main__":
    unittest.main()
