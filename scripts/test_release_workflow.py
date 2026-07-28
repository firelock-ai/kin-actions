"""Static contract tests for the reusable Cargo release workflow."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/cargo-registry-release.yml"
DEPENDENCY_WORKFLOW = ROOT / ".github/workflows/cargo-dependency-wave.yml"
HYGIENE_WORKFLOW = ROOT / ".github/workflows/public-history-hygiene.yml"


def _job_block(text: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  [a-zA-Z0-9_]+:\n|\Z)", text
    )
    if not match:
        raise AssertionError(f"job not found: {name}")
    return match.group(1)


def _job_condition(text: str, name: str) -> str:
    block = _job_block(text, name)
    folded = re.search(
        r"(?m)^    if: >-\n(?P<body>(?: {6,}.*\n)+)",
        block,
    )
    if folded:
        return " ".join(
            line.strip() for line in folded.group("body").splitlines()
        )
    inline = re.search(r"(?m)^    if: (?P<body>.+)$", block)
    if inline:
        return inline.group("body").strip()
    raise AssertionError(f"job has no condition: {name}")


def _step_block(job: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^      - name: {re.escape(name)}\n"
        rf"(?P<body>.*?)(?=^      - (?:name:|uses:)|\Z)",
        job,
    )
    if not match:
        raise AssertionError(f"step not found: {name}")
    return match.group("body")


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

    def test_durable_release_stages_are_exact_main_push_only(self) -> None:
        for job in (
            "publish",
            "consumer_smoke",
            "mint_release_tag",
            "dispatch_downstreams",
        ):
            with self.subTest(job=job):
                condition = _job_condition(self.text, job)
                self.assertIn("github.event_name == 'push'", condition)
                self.assertIn(
                    "github.ref == 'refs/heads/main'",
                    condition,
                )
                self.assertNotIn("refs/tags/", condition)
                self.assertNotIn("startsWith(github.ref", condition)

    def test_tag_triggered_release_candidate_cannot_publish(self) -> None:
        condition = _job_condition(self.text, "publish")
        self.assertEqual(
            condition,
            "github.event_name == 'push' && "
            "github.ref == 'refs/heads/main' && "
            "needs.version_gate.outputs.release_candidate == 'true'",
        )

        # Adversarial context: the version-moving commit is tagged and the
        # version gate still reports a candidate after comparing it with HEAD^.
        # The durable publication condition must nevertheless reject the tag.
        event_name = "push"
        ref = "refs/tags/v1.2.3"
        release_candidate = True
        admitted = (
            event_name == "push"
            and ref == "refs/heads/main"
            and release_candidate
        )
        self.assertFalse(admitted)

    def test_release_app_token_is_contents_write_only(self) -> None:
        mint = _job_block(self.text, "mint_release_tag")
        token_step = _step_block(mint, "Mint an App installation token")
        self.assertIn("uses: actions/create-github-app-token@v2", token_step)
        permissions = re.findall(
            r"(?m)^          (permission-[a-z-]+: .+)$",
            token_step,
        )
        self.assertEqual(permissions, ["permission-contents: write"])

    def test_branch_push_uses_guarded_event_before_as_version_base(self) -> None:
        version_gate = _job_block(self.text, "version_gate")
        self.assertIn(
            "PUSH_BEFORE: ${{ github.event_name == 'push' && "
            "github.ref_type == 'branch' && github.event.before || '' }}",
            version_gate,
        )

    def test_dependency_waves_are_serialized_and_losslessly_coalesced(self) -> None:
        dependency = DEPENDENCY_WORKFLOW.read_text()
        self.assertIn(
            "group: kin-dependency-wave-${{ github.repository }}", dependency
        )
        self.assertIn("cancel-in-progress: false", dependency)
        refresh_input = re.search(
            r"(?ms)^      refresh-all-on-event:\n"
            r"(.*?)(?=^      [a-zA-Z0-9_-]+:\n)",
            dependency,
        )
        self.assertIsNotNone(refresh_input)
        self.assertIn("default: true", refresh_input.group(1))
        self.assertNotIn("REFRESH_ALL", dependency)
        self.assertIn('for crate in $WATCHED', dependency)
        self.assertIn('--event-crate "$EVENT_CRATE"', dependency)
        self.assertIn('--version "$EVENT_VERSION"', dependency)

    def test_no_change_wave_cannot_open_or_auto_merge_a_pr(self) -> None:
        dependency = DEPENDENCY_WORKFLOW.read_text()
        self.assertIn(
            'if [[ "$code" == "0" && "$REPORT_ONLY" != "true" ]]; then',
            dependency,
        )
        self.assertRegex(
            dependency,
            r"(?ms)- name: Open dependency bump PR\n"
            r".*?if: steps\.update\.outputs\.changed == 'true'",
        )
        self.assertRegex(
            dependency,
            r"(?ms)- name: Arm auto-merge on the bump PR\n"
            r".*?inputs\.auto-merge && "
            r"steps\.update\.outputs\.changed == 'true'",
        )

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

    def test_helper_checkouts_bind_to_called_workflow_source(self) -> None:
        combined = "\n".join(
            (
                self.text,
                DEPENDENCY_WORKFLOW.read_text(),
                HYGIENE_WORKFLOW.read_text(),
            )
        )
        self.assertNotIn("ref: ${{ inputs.kin-actions-ref }}", combined)
        helper_count = combined.count("path: .kin-actions")
        self.assertGreater(helper_count, 0)
        self.assertEqual(
            combined.count("repository: ${{ job.workflow_repository }}"),
            helper_count,
        )
        self.assertEqual(
            combined.count("ref: ${{ job.workflow_sha }}"),
            helper_count,
        )


if __name__ == "__main__":
    unittest.main()
