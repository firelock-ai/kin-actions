"""Contract tests for the separate workflow-pin reconciliation authority."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("reconcile-workflow-pin-branch.py")
SPEC = importlib.util.spec_from_file_location("reconcile_workflow_pin_branch", SCRIPT)
pin_branch = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = pin_branch
SPEC.loader.exec_module(pin_branch)


class WorkflowPinAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manifest = self.root / "consumers.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_manifest(self, paths: list[str]) -> None:
        self.manifest.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "repositories": {"firelock-ai/example": paths},
                }
            ),
            encoding="utf-8",
        )

    def test_exact_manifest_workflow_paths_are_allowed(self) -> None:
        self.write_manifest(
            [
                ".github/workflows/a.yml",
                ".github/workflows/b.yaml",
            ]
        )
        self.assertEqual(
            pin_branch.allowed_paths(self.manifest, "firelock-ai/example"),
            [".github/workflows/a.yml", ".github/workflows/b.yaml"],
        )

    def test_general_release_validator_still_rejects_workflows(self) -> None:
        with self.assertRaises(pin_branch.rrb.InvariantError):
            pin_branch.rrb.validate_generated_paths(
                [".github/workflows/a.yml"]
            )

    def test_unlisted_repository_and_nonworkflow_paths_fail(self) -> None:
        self.write_manifest([".github/workflows/a.yml"])
        with self.assertRaises(pin_branch.pins.PinUpdateError):
            pin_branch.allowed_paths(self.manifest, "firelock-ai/other")

        with mock.patch.object(
            pin_branch.pins,
            "load_consumer_paths",
            return_value=["README.md"],
        ):
            with self.assertRaisesRegex(
                pin_branch.rrb.InvariantError, "outside workflow"
            ):
                pin_branch.allowed_paths(
                    self.manifest, "firelock-ai/example"
                )


if __name__ == "__main__":
    unittest.main()
