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

    def test_pending_run_replacement_cannot_drop_release_intent(self) -> None:
        # GitHub keeps only one pending member of a concurrency group. Both
        # release trains must therefore reconstruct intent from durable state,
        # never from whichever notification happened to survive.
        for name, text in (
            ("cargo", self.cargo),
            ("self", self.self_train),
        ):
            with self.subTest(name=name):
                self.assertIn(
                    'git rev-list --reverse "${BASE_REF}..HEAD"',
                    text,
                )
                self.assertIn(
                    '"repos/${GITHUB_REPOSITORY}/commits/${sha}/pulls"',
                    text,
                )
                self.assertIn(
                    'gh pr list --repo "$GITHUB_REPOSITORY" --state open',
                    text,
                )
                self.assertNotIn(
                    "github.event.client_payload.release_intent",
                    text,
                )

        # Publication and pin delivery likewise derive from durable current
        # state, so a later survivor subsumes any replaced pending event.
        self.assertIn(
            "version=\"$(tr -d '[:space:]' < VERSION)\"",
            self.release,
        )
        self.assertIn(
            'gh api "repos/${GITHUB_REPOSITORY}/releases/latest"',
            self.pin,
        )
        self.assertIn('print(*sorted(data["repositories"])', self.pin)
        self.assertIn("print(latest)", self.pin)

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
        self.assertIn('failures+=("$repository")', self.pin)
        self.assertIn("if ((${#failures[@]} > 0)); then", self.pin)

    def test_pin_updater_requires_an_exact_live_manifest(self) -> None:
        updater = read("scripts/update-kin-actions-pins.py")
        self.assertIn("def discover_pin_paths(", updater)
        self.assertIn("unmanifested live pins", updater)
        self.assertIn("manifest paths without live pins", updater)

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
        self.assertIn('jq -r .changed <<<"$neutralization"', self.reconcile)
        self.assertIn("git commit --allow-empty -s", self.reconcile)

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
        self.assertIn(
            'jq -r .changed <<<"$neutralization"', self.pin_reconcile
        )
        self.assertIn("git commit --allow-empty -s", self.pin_reconcile)
        self.assertIn(
            'git rev-parse "${head}^{tree}"', self.pin_reconcile
        )

    def test_auto_merge_is_bound_to_exact_generated_head(self) -> None:
        for script in (self.finalize, self.pin_reconcile):
            self.assertIn("--match-head-commit", script)
            self.assertIn("--auto --squash", script)

    def test_called_cargo_train_helpers_use_exact_workflow_identity(self) -> None:
        self.assertIn(
            "repository: ${{ job.workflow_repository }}", self.cargo
        )
        self.assertIn("ref: ${{ job.workflow_sha }}", self.cargo)

    def test_cargo_train_feeds_the_exact_generated_allowlist_to_policy(self) -> None:
        self.assertIn(
            "ALLOWED_PATHS_JSON: ${{ steps.state.outputs.allowed_paths_json }}",
            self.cargo,
        )
        self.assertIn("jq -r '.[]' <<<\"$ALLOWED_PATHS_JSON\"", self.cargo)
        self.assertIn(
            'generated_args+=(--generated-path "$path")',
            self.cargo,
        )
        self.assertIn('"${generated_args[@]}"', self.cargo)

    def test_release_controller_reconciles_tag_release_then_pin_event(self) -> None:
        mint = self.release.index("scripts/mint-release-tag.sh")
        github_release = self.release.index('gh release create "$TAG"')
        dispatch = self.release.index('"kin-actions-pin-reconcile"')
        self.assertLess(mint, github_release)
        self.assertLess(github_release, dispatch)
        self.assertIn('git show "${tag_sha}:VERSION"', self.release)

    def test_train_tags_are_bound_to_their_version_authority(self) -> None:
        self.assertIn("--inspect-ref \"$tag_sha\"", self.cargo)
        self.assertIn('git show "${tag_sha}:VERSION"', self.self_train)

    def test_cargo_tag_delivery_cannot_republish(self) -> None:
        registry = read(".github/workflows/cargo-registry-release.yml")
        publish = re.search(
            r"(?ms)^  publish:\n(.*?)(?=^  [a-zA-Z0-9_]+:\n|\Z)",
            registry,
        )
        self.assertIsNotNone(publish)
        block = publish.group(1)
        self.assertIn("github.ref == 'refs/heads/main'", block)
        self.assertNotIn("refs/tags", block)

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

    def test_activation_contract_limits_ruleset_bypass_to_bot_branches(self) -> None:
        readme = " ".join(read("README.md").split())
        self.assertIn("no Workflows permission or `main` bypass", readme)
        self.assertIn(
            "the exact `automation/release-next` branch bypass",
            readme,
        )
        self.assertIn(
            "only the exact `automation/kin-actions-pin-next` branch bypass",
            readme,
        )
        self.assertIn(
            "a second freeze ruleset blocks tag update, deletion, and "
            "non-fast-forward without any release-App bypass",
            readme,
        )


if __name__ == "__main__":
    unittest.main()
