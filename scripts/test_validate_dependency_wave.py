"""Adversarial tests for exact dependency-wave admission."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("validate-dependency-wave.py")


class DependencyWaveAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.write(
            "Cargo.toml",
            '[package]\nname = "demo"\nversion = "1.2.3"\n'
            '\n[dependencies]\nkin-db = { version = "0.6.6", registry = "kin" }\n',
        )
        self.write("Cargo.lock", "# tracked lock\n")
        self.write("README.md", "baseline\n")
        self.git("add", ".")
        self.git("commit", "-q", "-s", "-m", "baseline")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, relative: str, content: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            check=True,
            text=True,
            capture_output=True,
        )

    def run_validator(
        self,
        *,
        mode: str = "train",
        bump: bool = False,
        ephemeral: bool = False,
        expected_tree: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            "python3",
            str(SCRIPT),
            "--manifest",
            "Cargo.toml",
            "--version-mode",
            mode,
            "--bump-own-version",
            str(bump).lower(),
        ]
        if ephemeral:
            command.extend(["--ephemeral-path", ".kin-actions"])
        if expected_tree is not None:
            command.extend(["--expected-tree", expected_tree])
        return subprocess.run(
            command,
            cwd=self.root,
            check=False,
            text=True,
            capture_output=True,
        )

    def valid_dependency_change(self) -> None:
        manifest = (self.root / "Cargo.toml").read_text(encoding="utf-8")
        self.write("Cargo.toml", manifest.replace('"0.6.6"', '"0.7.0"'))
        self.write("Cargo.lock", "# tracked lock\n# kin-db 0.7.0\n")

    def test_exact_manifest_and_tracked_lock_are_staged_and_bound(self) -> None:
        self.valid_dependency_change()
        result = self.run_validator()
        self.assertEqual(result.returncode, 0, result.stderr)
        admission = json.loads(result.stdout)
        self.assertEqual(admission["paths"], ["Cargo.lock", "Cargo.toml"])
        self.assertEqual(admission["tree"], self.git("write-tree").stdout.strip())
        self.assertEqual(
            self.git("diff", "--cached", "--name-only").stdout.splitlines(),
            ["Cargo.lock", "Cargo.toml"],
        )

    def test_test_produced_tracked_file_is_rejected(self) -> None:
        self.valid_dependency_change()
        self.write("README.md", "test rewrote me\n")
        result = self.run_validator()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("README.md", result.stderr)
        self.assertEqual(self.git("diff", "--cached", "--name-only").stdout, "")

    def test_test_produced_untracked_file_is_rejected(self) -> None:
        self.valid_dependency_change()
        self.write("test-output.txt", "unexpected\n")
        result = self.run_validator()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("test-output.txt", result.stderr)

    def test_train_mode_rejects_own_version_movement(self) -> None:
        self.valid_dependency_change()
        manifest = (self.root / "Cargo.toml").read_text(encoding="utf-8")
        self.write("Cargo.toml", manifest.replace('version = "1.2.3"', 'version = "1.2.4"'))
        result = self.run_validator()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("own-version authority changed", result.stderr)

    def test_in_allowlist_test_mutation_differs_from_generated_tree(self) -> None:
        self.valid_dependency_change()
        generated = self.run_validator()
        self.assertEqual(generated.returncode, 0, generated.stderr)
        expected_tree = json.loads(generated.stdout)["tree"]
        manifest = (self.root / "Cargo.toml").read_text(encoding="utf-8")
        self.write("Cargo.toml", manifest + "\n[features]\nunexpected = []\n")
        verified = self.run_validator(expected_tree=expected_tree)
        self.assertNotEqual(verified.returncode, 0)
        self.assertIn("differs from generated tree", verified.stderr)

    def test_mode_change_is_rejected_even_on_allowlisted_manifest(self) -> None:
        self.valid_dependency_change()
        (self.root / "Cargo.toml").chmod(0o755)
        result = self.run_validator()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("changed file identity or mode", result.stderr)

    def test_manual_mode_can_explicitly_admit_own_version_movement(self) -> None:
        self.valid_dependency_change()
        manifest = (self.root / "Cargo.toml").read_text(encoding="utf-8")
        self.write("Cargo.toml", manifest.replace('version = "1.2.3"', 'version = "1.2.4"'))
        result = self.run_validator(mode="manual", bump=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_only_named_helper_checkout_is_ephemeral(self) -> None:
        self.valid_dependency_change()
        self.write(".kin-actions/helper.py", "temporary\n")
        accepted = self.run_validator(ephemeral=True)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

        self.git("reset", "-q")
        self.write("another-helper/file", "unexpected\n")
        rejected = self.run_validator(ephemeral=True)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("another-helper/file", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
