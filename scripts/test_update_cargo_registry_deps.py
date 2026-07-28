#!/usr/bin/env python3
"""Tests for the transactional Kin Cargo dependency-wave updater."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def _load(name, filename):
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ucd = _load("update_cargo_registry_deps", "update-cargo-registry-deps.py")

KIN_MANIFEST = """\
[dependencies]
kin-db = { version = "0.2.24", registry = "kin" }
serde = { version = "1", features = ["derive"] }
"""


class TemporaryManifestTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


class RequirementUpdates(unittest.TestCase):
    def test_preserves_supported_operators_and_spacing(self):
        for requirement, expected in (
            ("0.6.4", "0.7.0"),
            ("=0.6.4", "=0.7.0"),
            ("^0.6.4", "^0.7.0"),
            ("~0.6.4", "~0.7.0"),
            ("= 0.6.4", "= 0.7.0"),
        ):
            with self.subTest(requirement=requirement):
                self.assertEqual(
                    ucd.update_requirement(requirement, "0.7.0"), expected
                )

    def test_preserves_pre_release_and_build_target(self):
        self.assertEqual(
            ucd.update_requirement(
                "=0.6.4-alpha.1+build.7", "0.7.0-rc.1+release.2"
            ),
            "=0.7.0-rc.1+release.2",
        )

    def test_compound_and_malformed_requirements_fail_loud(self):
        for requirement in (
            ">=0.6, <0.7",
            "0.6 || 0.7",
            "*",
            ">0.6",
            "0.6.*",
            "",
        ):
            with self.subTest(requirement=requirement):
                with self.assertRaisesRegex(
                    ucd.UpdateError, "unsupported Cargo requirement"
                ):
                    ucd.update_requirement(requirement, "0.7.0")

    def test_malformed_target_version_fails_loud(self):
        for version in ("v0.7.0", ">=0.7", "", "0.7.*"):
            with self.subTest(version=version):
                with self.assertRaisesRegex(
                    ucd.UpdateError, "unsupported target version"
                ):
                    ucd.update_requirement("=0.6.4", version)


class ManifestRewriting(TemporaryManifestTest):
    def test_real_run_rewrites_kin_pin(self):
        path = self.write("Cargo.toml", KIN_MANIFEST)
        self.assertTrue(ucd.update_manifest(str(path), "kin-db", "0.2.25"))
        self.assertIn(
            'kin-db = { version = "0.2.25", registry = "kin" }',
            path.read_text(encoding="utf-8"),
        )

    def test_dry_run_reports_without_writing(self):
        path = self.write("Cargo.toml", KIN_MANIFEST)
        self.assertTrue(
            ucd.update_manifest(str(path), "kin-db", "0.2.25", dry_run=True)
        )
        self.assertEqual(path.read_text(encoding="utf-8"), KIN_MANIFEST)

    def test_non_kin_dependency_is_untouched(self):
        path = self.write("Cargo.toml", KIN_MANIFEST)
        self.assertFalse(ucd.update_manifest(str(path), "serde", "2"))
        self.assertEqual(path.read_text(encoding="utf-8"), KIN_MANIFEST)

    def test_already_current_pin_is_noop(self):
        path = self.write("Cargo.toml", KIN_MANIFEST)
        self.assertFalse(ucd.update_manifest(str(path), "kin-db", "0.2.24"))
        self.assertEqual(path.read_text(encoding="utf-8"), KIN_MANIFEST)

    def test_compact_single_quoted_inline_table_is_supported(self):
        path = self.write(
            "Cargo.toml",
            "[dependencies]\n"
            "model={package='kin-model',version='=0.6.4',registry='kin'}\n",
        )
        self.assertTrue(ucd.update_manifest(str(path), "kin-model", "0.7.0"))
        self.assertEqual(
            path.read_text(encoding="utf-8"),
            "[dependencies]\n"
            "model={package='kin-model',version='=0.7.0',registry='kin'}\n",
        )

    def test_inline_alias_preserves_operator(self):
        path = self.write(
            "Cargo.toml",
            "[dependencies]\n"
            'model = { package = "kin-model", version = "~0.6.4", '
            'registry = "kin" }\n',
        )
        self.assertTrue(ucd.update_manifest(str(path), "kin-model", "0.7.0"))
        self.assertIn(
            'version = "~0.7.0"', path.read_text(encoding="utf-8")
        )

    def test_dependency_table_form_and_alias_are_supported(self):
        path = self.write(
            "Cargo.toml",
            "[workspace.dependencies.model]\n"
            "package = 'kin-model'\n"
            "version = '=0.6.4' # exact by policy\n"
            "registry = 'kin'\n",
        )
        self.assertTrue(ucd.update_manifest(str(path), "kin-model", "0.7.0"))
        self.assertIn(
            "version = '=0.7.0' # exact by policy",
            path.read_text(encoding="utf-8"),
        )

    def test_target_dependency_table_is_supported(self):
        path = self.write(
            "Cargo.toml",
            "[target.'cfg(unix)'.dependencies.kin-db]\n"
            'version="^0.6.4"\n'
            'registry="kin"\n',
        )
        self.assertTrue(ucd.update_manifest(str(path), "kin-db", "0.7.0"))
        self.assertIn('version="^0.7.0"', path.read_text(encoding="utf-8"))

    def test_root_dotted_dependency_key_is_supported(self):
        path = self.write(
            "Cargo.toml",
            "dependencies.kin-model={version='=0.6.4',registry='kin'}\n",
        )
        self.assertTrue(ucd.update_manifest(str(path), "kin-model", "0.7.0"))
        self.assertIn("version='=0.7.0'", path.read_text(encoding="utf-8"))

    def test_compound_requirement_fails_without_writing(self):
        manifest = (
            "[dependencies]\n"
            'kin-model = { version = ">=0.6, <0.7", registry = "kin" }\n'
        )
        path = self.write("Cargo.toml", manifest)
        with self.assertRaisesRegex(
            ucd.UpdateError, "unsupported Cargo requirement"
        ):
            ucd.update_manifest(str(path), "kin-model", "0.7.0")
        self.assertEqual(path.read_text(encoding="utf-8"), manifest)

    def test_escaped_version_literal_fails_without_writing(self):
        manifest = (
            "[dependencies]\n"
            'kin-model = { version = "\\u003d0.6.4", registry = "kin" }\n'
        )
        path = self.write("Cargo.toml", manifest)
        with self.assertRaisesRegex(ucd.UpdateError, "escaped or ambiguous"):
            ucd.update_manifest(str(path), "kin-model", "0.7.0")
        self.assertEqual(path.read_text(encoding="utf-8"), manifest)

    def test_multiline_version_literal_fails_without_writing(self):
        manifest = (
            "[dependencies.kin-model]\n"
            'version = """=0.6.4"""\n'
            'registry = "kin"\n'
        )
        path = self.write("Cargo.toml", manifest)
        with self.assertRaisesRegex(ucd.UpdateError, "unsupported table formatting"):
            ucd.update_manifest(str(path), "kin-model", "0.7.0")
        self.assertEqual(path.read_text(encoding="utf-8"), manifest)

    def test_registry_dependency_without_string_version_fails(self):
        manifest = (
            "[dependencies]\n"
            'kin-model = { version = 640, registry = "kin" }\n'
        )
        path = self.write("Cargo.toml", manifest)
        with self.assertRaisesRegex(ucd.UpdateError, "has no string version"):
            ucd.update_manifest(str(path), "kin-model", "0.7.0")
        self.assertEqual(path.read_text(encoding="utf-8"), manifest)

    def test_malformed_toml_fails_without_writing(self):
        manifest = (
            "[dependencies]\n"
            'kin-model = { version = "=0.6.4", registry = "kin" \n'
        )
        path = self.write("Cargo.toml", manifest)
        with self.assertRaisesRegex(ucd.UpdateError, "malformed TOML"):
            ucd.update_manifest(str(path), "kin-model", "0.7.0")
        self.assertEqual(path.read_text(encoding="utf-8"), manifest)


class TransactionalUpdates(TemporaryManifestTest):
    def test_all_manifests_are_planned_before_any_write(self):
        first_text = (
            "[dependencies]\n"
            'kin-model = { version = "=0.6.4", registry = "kin" }\n'
        )
        second_text = (
            "[dependencies]\n"
            'kin-model = { version = ">=0.6, <0.7", registry = "kin" }\n'
        )
        first = self.write("Cargo.toml", first_text)
        second = self.write("crates/member/Cargo.toml", second_text)

        with self.assertRaisesRegex(
            ucd.UpdateError, "unsupported Cargo requirement"
        ):
            ucd.plan_manifests([first, second], {"kin-model": "0.7.0"})

        self.assertEqual(first.read_text(encoding="utf-8"), first_text)
        self.assertEqual(second.read_text(encoding="utf-8"), second_text)

    def test_multi_manifest_multi_crate_update_is_one_plan(self):
        first = self.write(
            "Cargo.toml",
            "[workspace.dependencies]\n"
            'kin-model = { version = "=0.6.4", registry = "kin" }\n',
        )
        second = self.write(
            "crates/member/Cargo.toml",
            "[dependencies]\n"
            'kin-db = { version = "~0.6.6", registry = "kin" }\n',
        )
        lock = self.write("Cargo.lock", "lock-before\n")
        calls = []

        def cargo_run(command, check):
            calls.append((command, check))
            return subprocess.CompletedProcess(command, 0)

        versions = {"kin-model": "0.7.0", "kin-db": "0.6.7"}
        plans = ucd.plan_manifests([first, second], versions)
        changes = ucd.apply_plans(
            plans, versions, lock_path=lock, cargo_run=cargo_run
        )

        self.assertEqual(len(changes), 2)
        self.assertIn('version = "=0.7.0"', first.read_text(encoding="utf-8"))
        self.assertIn('version = "~0.6.7"', second.read_text(encoding="utf-8"))
        self.assertEqual(
            calls,
            [
                (
                    ["cargo", "update", "-p", "kin-model", "--precise", "0.7.0"],
                    True,
                ),
                (
                    ["cargo", "update", "-p", "kin-db", "--precise", "0.6.7"],
                    True,
                ),
            ],
        )

    def test_cargo_failure_restores_all_manifests_and_lockfile(self):
        first_text = (
            "[dependencies]\n"
            'kin-model = { version = "=0.6.4", registry = "kin" }\n'
        )
        second_text = (
            "[dependencies]\n"
            'kin-model = { version = "=0.6.4", registry = "kin" }\n'
        )
        first = self.write("Cargo.toml", first_text)
        second = self.write("crates/member/Cargo.toml", second_text)
        lock = self.write("Cargo.lock", "lock-before\n")

        def cargo_run(command, check):
            lock.write_text("partially-updated-lock\n", encoding="utf-8")
            raise subprocess.CalledProcessError(101, command)

        versions = {"kin-model": "0.7.0"}
        plans = ucd.plan_manifests([first, second], versions)
        with self.assertRaisesRegex(
            ucd.UpdateError, "manifests and Cargo.lock restored"
        ):
            ucd.apply_plans(
                plans, versions, lock_path=lock, cargo_run=cargo_run
            )

        self.assertEqual(first.read_text(encoding="utf-8"), first_text)
        self.assertEqual(second.read_text(encoding="utf-8"), second_text)
        self.assertEqual(lock.read_text(encoding="utf-8"), "lock-before\n")

    def test_dry_run_never_writes_or_invokes_cargo(self):
        manifest = (
            "[dependencies]\n"
            'kin-model = { version = "=0.6.4", registry = "kin" }\n'
        )
        path = self.write("Cargo.toml", manifest)
        lock = self.write("Cargo.lock", "lock-before\n")

        def cargo_run(*_args, **_kwargs):
            raise AssertionError("cargo must not run in dry-run mode")

        versions = {"kin-model": "0.7.0"}
        plans = ucd.plan_manifests([path], versions)
        changes = ucd.apply_plans(
            plans,
            versions,
            dry_run=True,
            lock_path=lock,
            cargo_run=cargo_run,
        )

        self.assertEqual(len(changes), 1)
        self.assertEqual(path.read_text(encoding="utf-8"), manifest)
        self.assertEqual(lock.read_text(encoding="utf-8"), "lock-before\n")

    def test_missing_manifest_aborts_planning(self):
        with self.assertRaisesRegex(ucd.UpdateError, "manifest does not exist"):
            ucd.plan_manifests(
                [self.root / "missing.toml"], {"kin-model": "0.7.0"}
            )


class WorkflowContract(unittest.TestCase):
    def test_updater_failure_aborts_and_refresh_is_single_transaction(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/cargo-dependency-wave.yml"
        ).read_text(encoding="utf-8")

        self.assertIn('case "$result" in', workflow)
        self.assertIn('0|2) return "$result"', workflow)
        self.assertIn('exit "$result"', workflow)
        self.assertIn('crate_args+=(--crate "$crate")', workflow)
        self.assertEqual(
            workflow.count(
                "python3 .kin-actions/scripts/update-cargo-registry-deps.py"
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
