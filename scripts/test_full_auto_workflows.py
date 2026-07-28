"""Static security and authority contracts for full-auto release workflows."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def token_block(text: str, step_id: str) -> str:
    match = re.search(
        rf"(?ms)^      - name: .*?\n"
        rf"(?:        .*\n)*?"
        rf"        id: {re.escape(step_id)}\n"
        rf".*?(?=^      - |\Z)",
        text,
    )
    if not match:
        raise AssertionError(f"token step not found: {step_id}")
    return match.group(0)


class FullAutoWorkflowContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cargo = read(".github/workflows/cargo-release-train.yml")
        cls.self_train = read(".github/workflows/self-release-train.yml")
        cls.release = read(".github/workflows/release.yml")
        cls.pin = read(".github/workflows/pin-wave.yml")
        cls.reconcile = read("scripts/reconcile-release-pr.sh")
        cls.finalize = read("scripts/finalize-release-pr.sh")
        cls.pin_reconcile = read("scripts/reconcile-workflow-pin-pr.sh")

    def test_controllers_are_default_branch_event_surfaces(self) -> None:
        for name, text in (
            ("self train", self.self_train),
            ("release", self.release),
            ("pin", self.pin),
        ):
            with self.subTest(name=name):
                self.assertNotIn("workflow_dispatch:", text)
                self.assertIn("schedule:", text)
        self.assertIn("workflow_run:", self.self_train)
        self.assertIn("workflow_run:", self.release)
        self.assertIn("repository_dispatch:", self.pin)

    def test_every_train_is_serialized_without_cancellation(self) -> None:
        for text in (self.cargo, self.self_train, self.release, self.pin):
            self.assertIn("concurrency:", text)
            self.assertIn("cancel-in-progress: false", text)

    def test_general_release_tokens_cannot_edit_workflows(self) -> None:
        for name, text in (
            ("cargo", self.cargo),
            ("self", self.self_train),
            ("release", self.release),
        ):
            with self.subTest(name=name):
                self.assertIn("permission-contents: write", text)
                self.assertNotIn("permission-workflows:", text)
        self.assertIn("permission-workflows: write", self.pin)

    def test_pin_controller_uses_separate_environment_and_secret_contract(self) -> None:
        self.assertIn("environment: release-followups", self.pin)
        self.assertIn("KIN_WORKFLOW_PIN_APP_ID", self.pin)
        self.assertIn("KIN_WORKFLOW_PIN_APP_PRIVATE_KEY", self.pin)
        self.assertNotIn("KIN_RELEASE_BOT_APP_ID", self.pin)
        self.assertIn(".kin-release/consumers.json", self.pin)
        self.assertIn("installation/repositories", self.pin)

    def test_general_train_crosses_main_only_through_server_merge(self) -> None:
        neutralize = self.reconcile.index(" neutralize ")
        post_merge = self.reconcile.index(
            'gh api --method POST "repos/${GITHUB_REPOSITORY}/merges"'
        )
        validate = self.reconcile.index(" validate-merge ")
        self.assertLess(neutralize, post_merge)
        self.assertLess(post_merge, validate)
        self.assertNotRegex(
            self.reconcile,
            r"(?m)git\s+push[^\n]*(?:--force|-f\b)",
        )
        self.assertNotIn("--method DELETE", self.reconcile)
        self.assertNotIn("refs/heads/${TRAIN_BRANCH}\" -f force=true", self.reconcile)

    def test_pin_train_never_deletes_or_force_rewrites(self) -> None:
        self.assertIn(
            'gh api --method POST "repos/${TARGET_REPOSITORY}/merges"',
            self.pin_reconcile,
        )
        self.assertNotRegex(
            self.pin_reconcile,
            r"(?m)git\s+push[^\n]*(?:--force|-f\b)",
        )
        self.assertNotIn("--method DELETE", self.pin_reconcile)
        self.assertNotIn("delete-branch", self.pin_reconcile)

    def test_auto_merge_is_bound_to_exact_generated_head(self) -> None:
        for script in (self.finalize, self.pin_reconcile):
            self.assertIn("--match-head-commit", script)
            self.assertIn("--auto --squash", script)

    def test_called_cargo_train_helpers_use_exact_workflow_identity(self) -> None:
        self.assertIn(
            "repository: ${{ job.workflow_repository }}", self.cargo
        )
        self.assertIn("ref: ${{ job.workflow_sha }}", self.cargo)

    def test_release_controller_reconciles_tag_release_then_pin_event(self) -> None:
        mint = self.release.index("scripts/mint-release-tag.sh")
        github_release = self.release.index('gh release create "$TAG"')
        dispatch = self.release.index('"kin-actions-pin-reconcile"')
        self.assertLess(mint, github_release)
        self.assertLess(github_release, dispatch)

    def test_manifest_contains_every_live_external_consumer_path(self) -> None:
        manifest = read(".kin-release/consumers.json")
        expected = (
            "firelock-ai/kin",
            "firelock-ai/kin-bench",
            "firelock-ai/kinlab",
            "firelock-ai/kin-model",
            "firelock-ai/kin-db",
            "firelock-ai/kin-lsp",
            "firelock-ai/kin-vfs",
            "firelock-ai/kin-infer",
            "firelock-ai/kin-vector",
            "firelock-ai/kin-search",
            "firelock-ai/kin-blobs",
        )
        for repository in expected:
            self.assertIn(f'"{repository}"', manifest)


if __name__ == "__main__":
    unittest.main()
