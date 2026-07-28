"""Adversarial tests for kin-actions self-release authority."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("check-self-release.py")
SPEC = importlib.util.spec_from_file_location("check_self_release", SCRIPT)
gate = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = gate
SPEC.loader.exec_module(gate)


class SelfReleaseAuthorityTests(unittest.TestCase):
    def evaluate(self, **changes):
        values = {
            "current_version": "0.1.22",
            "base_version": "0.1.22",
            "changed_paths": ["scripts/tool.py"],
            "event_name": "pull_request",
            "ref_type": "branch",
            "ref_name": "feature",
            "default_branch": "main",
            "base_repo": "firelock-ai/kin-actions",
            "head_repo": "firelock-ai/kin-actions",
            "head_branch": "feature",
            "labels": [],
            "remote_tag_sha": None,
            "head_sha": "a" * 40,
            "pin_failures": [],
        }
        values.update(changes)
        return gate.evaluate(**values)

    def generated(self, **changes):
        values = {
            "current_version": "0.1.23",
            "changed_paths": list(gate.ALLOWED_PATHS),
            "head_branch": gate.TRAIN_BRANCH,
            "labels": ["release:automated", "release:patch"],
        }
        values.update(changes)
        return self.evaluate(**values)

    def test_ordinary_pr_cannot_move_version(self) -> None:
        result = self.evaluate(
            current_version="0.1.23",
            changed_paths=["VERSION"],
        )
        self.assertTrue(any("only the exact" in item for item in result["failures"]))

    def test_exact_first_party_generated_pr_is_candidate(self) -> None:
        result = self.generated()
        self.assertEqual(result["failures"], [])
        self.assertTrue(result["release_candidate"])
        self.assertEqual(result["intent"], "patch")

    def test_fork_label_branch_and_extra_path_each_fail(self) -> None:
        cases = (
            {"head_repo": "attacker/kin-actions"},
            {"labels": ["release:patch"]},
            {"head_branch": "automation/lookalike"},
            {"changed_paths": [*gate.ALLOWED_PATHS, "scripts/tool.py"]},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertTrue(self.generated(**changes)["failures"])

    def test_patch_minor_major_must_be_exact_successors(self) -> None:
        for version, intent in (
            ("0.1.23", "patch"),
            ("0.2.0", "minor"),
            ("1.0.0", "major"),
        ):
            with self.subTest(version=version):
                result = self.generated(current_version=version)
                self.assertEqual(result["failures"], [])
                self.assertEqual(result["intent"], intent)
        self.assertTrue(
            self.generated(current_version="0.1.24")["failures"]
        )

    def test_existing_remote_tag_blocks_pr(self) -> None:
        result = self.generated(remote_tag_sha="b" * 40)
        self.assertTrue(any("already exists" in item for item in result["failures"]))

    def test_main_push_allows_exact_generated_successor(self) -> None:
        result = self.generated(
            event_name="push",
            ref_name="main",
            head_branch="",
            labels=[],
        )
        self.assertEqual(result["failures"], [])
        self.assertTrue(result["release_candidate"])

    def test_main_rerun_allows_tag_only_at_exact_head(self) -> None:
        exact = self.generated(
            event_name="push",
            ref_name="main",
            remote_tag_sha="a" * 40,
            head_branch="",
            labels=[],
        )
        self.assertEqual(exact["failures"], [])
        wrong = self.generated(
            event_name="push",
            ref_name="main",
            remote_tag_sha="b" * 40,
            head_branch="",
            labels=[],
        )
        self.assertTrue(any("not exact head" in item for item in wrong["failures"]))

    def test_non_default_push_and_pin_mismatch_fail(self) -> None:
        branch = self.generated(
            event_name="push",
            ref_name="other",
            head_branch="",
            labels=[],
        )
        self.assertTrue(any("default branch" in item for item in branch["failures"]))
        pins = self.generated(pin_failures=["README pin mismatch"])
        self.assertIn("README pin mismatch", pins["failures"])

    def test_no_version_movement_is_not_a_release_candidate(self) -> None:
        result = self.evaluate()
        self.assertEqual(result["failures"], [])
        self.assertFalse(result["release_candidate"])

    def test_document_pins_must_all_equal_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, version in (
                ("README.md", "0.1.23"),
                ("CONTRIBUTING.md", "0.1.22"),
            ):
                (root / name).write_text(
                    "uses: firelock-ai/kin-actions/.github/workflows/"
                    f"x.yml@v{version}\n",
                    encoding="utf-8",
                )
            failures = gate.validate_document_pins(root, "0.1.23")
            self.assertTrue(any("CONTRIBUTING.md" in item for item in failures))

    def test_base_selection_fails_closed_without_exact_branch_before(self) -> None:
        with self.assertRaises(gate.SelfReleaseGateError):
            gate.select_base_ref("", "", "branch")
        with self.assertRaises(gate.SelfReleaseGateError):
            gate.select_base_ref("", "0" * 40, "branch")
        with self.assertRaises(gate.SelfReleaseGateError):
            gate.select_base_ref("", "a" * 40, "tag")


if __name__ == "__main__":
    unittest.main()
