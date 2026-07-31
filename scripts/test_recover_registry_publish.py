"""Adversarial tests for exact pre-publication registry-run recovery."""

from __future__ import annotations

import importlib.util
import unittest
from contextlib import ExitStack
from unittest.mock import patch
from pathlib import Path


SCRIPT = Path(__file__).with_name("recover-registry-publish.py")
SPEC = importlib.util.spec_from_file_location("recover_registry_publish", SCRIPT)
assert SPEC and SPEC.loader
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)

HEAD = "a" * 40


class RegistryPublishRecoveryTests(unittest.TestCase):
    def workflow_run(
        self,
        *,
        status: str = "completed",
        conclusion: str = "failure",
        attempt: int = 1,
        head: str = HEAD,
        branch: str = "main",
    ) -> list[dict[str, object]]:
        return [
            {
                "attempt": attempt,
                "conclusion": conclusion,
                "createdAt": "2026-07-28T00:00:00Z",
                "databaseId": 123,
                "event": "push",
                "headBranch": branch,
                "headSha": head,
                "status": status,
                "url": "https://github.com/firelock-ai/demo/actions/runs/123",
                "workflowName": "Registry Publish",
            }
        ]

    def execute(
        self,
        registry_states: list[str],
        runs: list[list[dict[str, object]]],
        *,
        visibility: int = 0,
        max_attempts: int = 3,
        monotonic_values: list[float] | None = None,
    ):
        state_values = iter(registry_states)
        run_values = iter(runs)
        reruns: list[tuple[str, ...]] = []

        def fake_registry(**_kwargs):
            return {"state": next(state_values)}

        def fake_json(_args, _token):
            return next(run_values)

        def fake_no_content(args, _token):
            reruns.append(tuple(args))

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    recovery,
                    "inspect_registry",
                    side_effect=fake_registry,
                )
            )
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
                package="kin-demo",
                version="1.2.3",
                version_commit=HEAD,
                workflow="Registry Publish",
                default_branch="main",
                registry_url="https://kinlab.ai",
                helper_root=Path("/helper"),
                actions_token="actions",
                timeout_seconds=1,
                visibility_seconds=visibility,
                poll_seconds=0,
                max_attempts=max_attempts,
            )
        return state, reruns

    def test_failed_run_is_rerun_then_available_row_wins(self) -> None:
        state, reruns = self.execute(
            ["version-absent", "available"],
            [self.workflow_run()],
        )
        self.assertEqual(state, "available")
        self.assertEqual(len(reruns), 1)
        self.assertIn("rerun-failed-jobs", reruns[0][-1])

    def test_row_becoming_visible_prevents_rerun(self) -> None:
        state, reruns = self.execute(
            ["version-absent", "available"],
            [self.workflow_run()],
            visibility=60,
        )
        self.assertEqual(state, "available")
        self.assertEqual(reruns, [])

    def test_cancelled_run_uses_bounded_full_rerun(self) -> None:
        state, reruns = self.execute(
            ["version-absent", "available"],
            [self.workflow_run(conclusion="cancelled")],
        )
        self.assertEqual(state, "available")
        self.assertEqual(len(reruns), 1)
        self.assertTrue(reruns[0][-1].endswith("/rerun"))

    def test_action_required_run_has_no_automatic_bypass(self) -> None:
        with self.assertRaisesRegex(
            recovery.PublicationRecoveryError,
            "has no safe automatic rerun",
        ):
            self.execute(
                ["version-absent"],
                [self.workflow_run(conclusion="action_required")],
            )

    def test_running_run_waits_without_rerun(self) -> None:
        state, reruns = self.execute(
            ["version-absent", "available"],
            [self.workflow_run(status="in_progress", conclusion="")],
        )
        self.assertEqual(state, "available")
        self.assertEqual(reruns, [])

    def test_available_row_never_touches_actions(self) -> None:
        state, reruns = self.execute(["available"], [])
        self.assertEqual(state, "available")
        self.assertEqual(reruns, [])

    def test_yanked_row_is_terminal(self) -> None:
        with self.assertRaisesRegex(
            recovery.PublicationRecoveryError,
            "yanked",
        ):
            self.execute(["yanked"], [])

    def test_attempt_exhaustion_is_terminal(self) -> None:
        with self.assertRaisesRegex(
            recovery.PublicationRecoveryError,
            "exhausted bounded attempt",
        ):
            self.execute(
                ["version-absent"],
                [self.workflow_run(attempt=3)],
                max_attempts=3,
            )

    def test_missing_exact_registry_run_times_out_without_mutation(self) -> None:
        with self.assertRaisesRegex(
            recovery.PublicationRecoveryError,
            "did not recover within",
        ):
            self.execute(
                ["version-absent"],
                [[]],
                monotonic_values=[0.0, 0.0, 2.0],
            )

    def test_successful_run_without_registry_row_is_not_rerun(self) -> None:
        with self.assertRaisesRegex(
            recovery.PublicationRecoveryError,
            "did not recover within",
        ):
            self.execute(
                ["version-absent"],
                [self.workflow_run(conclusion="success")],
                monotonic_values=[0.0, 0.0, 2.0],
            )


if __name__ == "__main__":
    unittest.main()
