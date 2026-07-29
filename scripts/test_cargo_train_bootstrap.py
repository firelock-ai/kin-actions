"""Contracts for the safe two-release Cargo train activation."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CargoTrainBootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bootstrap = json.loads(
            (ROOT / ".kin-release/cargo-train-bootstrap.json").read_text(
                encoding="utf-8"
            )
        )
        cls.consumers = json.loads(
            (ROOT / ".kin-release/consumers.json").read_text(
                encoding="utf-8"
            )
        )
        cls.template = (
            ROOT / ".kin-release/release-train-caller.yml"
        ).read_text(encoding="utf-8")

    def test_exact_eight_caller_package_and_manifest_contracts(self) -> None:
        rows = self.bootstrap["callers"]
        self.assertEqual(self.bootstrap["schema"], 1)
        self.assertEqual(len(rows), 8)
        expected = {
            "firelock-ai/kin-blobs": ("kin-blobs", "package"),
            "firelock-ai/kin-search": ("kin-search", "package"),
            "firelock-ai/kin-vector": ("kin-vector", "package"),
            "firelock-ai/kin-infer": ("kin-infer", "package"),
            "firelock-ai/kin-model": ("kin-model", "package"),
            "firelock-ai/kin-db": ("kin-db", "workspace.package"),
            "firelock-ai/kin-lsp": ("kin-lsp", "package"),
            "firelock-ai/kin-vfs": ("kin-vfs-core", "workspace.package"),
        }
        self.assertEqual(
            {
                row["repository"]: (
                    row["package"],
                    row["version_authority"],
                )
                for row in rows
            },
            expected,
        )
        self.assertTrue(all(row["manifest"] == "Cargo.toml" for row in rows))

    def test_four_dependency_callers_disable_own_version_bumps(self) -> None:
        configured = {
            row["repository"]: row["dependency_wave"]
            for row in self.bootstrap["callers"]
            if row["dependency_wave"] is not None
        }
        self.assertEqual(
            set(configured),
            {
                "firelock-ai/kin-model",
                "firelock-ai/kin-db",
                "firelock-ai/kin-lsp",
                "firelock-ai/kin-vfs",
            },
        )
        for contract in configured.values():
            self.assertEqual(
                contract["path"],
                ".github/workflows/kin-dependency-wave.yml",
            )
            self.assertEqual(contract["version_mode"], "train")
            self.assertIs(contract["bump_own_version"], False)

    def test_future_caller_paths_are_deliberately_not_yet_in_inventory(self) -> None:
        future = self.bootstrap["future_caller_path"]
        for row in self.bootstrap["callers"]:
            self.assertNotIn(
                future,
                self.consumers["repositories"][row["repository"]],
            )

    def test_combined_caller_has_automatic_train_and_recovery_triggers(self) -> None:
        self.assertNotIn("workflow_dispatch:", self.template)
        self.assertIn('workflows: ["CI", "Registry Publish"]', self.template)
        self.assertIn("repository_dispatch:", self.template)
        self.assertIn("schedule:", self.template)
        self.assertIn(
            ".github/workflows/cargo-release-train.yml@vRELEASE_A",
            self.template,
        )
        self.assertIn(
            ".github/workflows/cargo-release-recovery.yml@vRELEASE_A",
            self.template,
        )
        self.assertEqual(self.template.count("secrets: inherit"), 2)


if __name__ == "__main__":
    unittest.main()
