"""Static contract tests for the reusable Cargo release workflow."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/cargo-registry-release.yml"
DEPENDENCY_WORKFLOW = ROOT / ".github/workflows/cargo-dependency-wave.yml"


def _job_block(text: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  [a-zA-Z0-9_]+:\n|\Z)", text
    )
    if not match:
        raise AssertionError(f"job not found: {name}")
    return match.group(1)


class ReleaseWorkflowContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text()

    def test_credential_bearing_jobs_share_publish_environment(self) -> None:
        for job in ("publish", "mint_release_tag", "dispatch_downstreams"):
            with self.subTest(job=job):
                self.assertIn(
                    "environment: ${{ inputs.publish-environment }}",
                    _job_block(self.text, job),
                )

    def test_only_version_moving_commit_enters_release_authority(self) -> None:
        version_gate = _job_block(self.text, "version_gate")
        publish = _job_block(self.text, "publish")
        self.assertIn(
            "release_candidate: ${{ steps.version_policy.outputs.release_candidate }}",
            version_gate,
        )
        self.assertRegex(version_gate, r"(?m)^        id: version_policy$")
        self.assertIn(
            "needs.version_gate.outputs.release_candidate == 'true'", publish
        )

    def test_dependency_waves_are_serialized_and_coalesced(self) -> None:
        dependency = DEPENDENCY_WORKFLOW.read_text()
        self.assertIn(
            "group: kin-dependency-wave-${{ github.repository }}", dependency
        )
        self.assertIn("cancel-in-progress: false", dependency)

    def test_migration_compatibility_secrets_remain_in_interface(self) -> None:
        for secret in (
            "KINLAB_CARGO_TOKEN",
            "KIN_CI_BOT_TOKEN",
            "KIN_DOWNSTREAM_DISPATCH_TOKEN",
            "KIN_RELEASE_TAG_TOKEN",
            "KIN_RELEASE_BOT_APP_ID",
            "KIN_RELEASE_BOT_PRIVATE_KEY",
        ):
            with self.subTest(secret=secret):
                self.assertRegex(self.text, rf"(?m)^      {secret}:$")

    def test_dispatch_waits_for_optional_tag_outcome(self) -> None:
        dispatch = _job_block(self.text, "dispatch_downstreams")
        self.assertIn("needs: [publish, consumer_smoke, mint_release_tag]", dispatch)
        self.assertIn("always() &&", dispatch)
        self.assertIn("needs.mint_release_tag.result == 'success'", dispatch)
        self.assertIn("needs.mint_release_tag.result == 'skipped'", dispatch)
        self.assertIn("Rerun only the failed dispatch job", dispatch)

    def test_mint_exposes_exact_completion_status(self) -> None:
        mint = _job_block(self.text, "mint_release_tag")
        self.assertIn(
            "release_tag_status: ${{ steps.mint.outputs.release_tag_status }}", mint
        )
        self.assertRegex(mint, r"(?m)^        id: mint$")


if __name__ == "__main__":
    unittest.main()
