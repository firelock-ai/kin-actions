#!/usr/bin/env python3
"""Tests for the transactional Kin Cargo dependency-wave updater."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock


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
    def test_semver_precedence_prefers_stable_and_orders_prereleases(self):
        versions = [
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-alpha.beta",
            "1.0.0-beta",
            "1.0.0-beta.2",
            "1.0.0-beta.11",
            "1.0.0-rc.1",
            "1.0.0",
        ]
        self.assertEqual(sorted(versions, key=ucd.parse_version), versions)
        self.assertEqual(
            max(
                ["1.0.0-alpha+build.9", "1.0.0+build.1"],
                key=ucd.parse_version,
            ),
            "1.0.0+build.1",
        )

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

    def test_stale_event_cannot_downgrade_a_current_requirement(self):
        for requirement in ("0.8.0", "=0.8.0", "^0.8", "~0.8.1"):
            with self.subTest(requirement=requirement):
                self.assertEqual(
                    ucd.update_requirement(requirement, "0.7.0"), requirement
                )

    def test_compound_and_malformed_requirements_fail_loud(self):
        for requirement in (
            ">=0.6, <0.7",
            "0.6 || 0.7",
            "*",
            ">0.6",
            "0.6.*",
            "01.6.4",
            "0.6.4-01",
            "0.6.4-alpha..1",
            "0.6-alpha",
            "0.6.4+",
            "",
        ):
            with self.subTest(requirement=requirement):
                with self.assertRaisesRegex(
                    ucd.UpdateError, "unsupported Cargo requirement"
                ):
                    ucd.update_requirement(requirement, "0.7.0")

    def test_malformed_target_version_fails_loud(self):
        for version in (
            "v0.7.0",
            ">=0.7",
            "",
            "0.7.*",
            "0.7",
            "01.7.0",
            "0.7.0-01",
            "0.7.0-",
            "0.7.0+",
        ):
            with self.subTest(version=version):
                with self.assertRaisesRegex(
                    ucd.UpdateError, "unsupported target version"
                ):
                    ucd.update_requirement("=0.6.4", version)


class RegistryVersions(unittest.TestCase):
    def test_latest_version_uses_semver_and_ignores_yanked_entries(self):
        body = "\n".join(
            (
                '{"vers":"1.0.0-alpha.1","yanked":false}',
                '{"vers":"1.0.0","yanked":false}',
                '{"vers":"2.0.0","yanked":true}',
            )
        ).encode("utf-8")
        response = mock.Mock()
        response.read.return_value = body
        with mock.patch.object(
            ucd.urllib.request, "urlopen", return_value=response
        ) as urlopen:
            self.assertEqual(
                ucd.latest_version("https://kinlab.ai", "kin-model"), "1.0.0"
            )
        urlopen.assert_called_once_with(
            "https://kinlab.ai/registry/cargo/ki/n-/kin-model", timeout=10
        )

    def test_invalid_registry_version_fails_loud(self):
        response = mock.Mock()
        response.read.return_value = b'{"vers":"01.0.0","yanked":false}\n'
        with mock.patch.object(
            ucd.urllib.request, "urlopen", return_value=response
        ):
            with self.assertRaisesRegex(
                ucd.UpdateError, "registry returned invalid SemVer"
            ):
                ucd.latest_version("https://kinlab.ai", "kin-model")

    def test_stale_event_coalesces_to_registry_latest(self):
        with mock.patch.object(ucd, "latest_version", return_value="0.8.0"):
            self.assertEqual(
                ucd._resolve_versions(
                    ["kin-model"], "0.7.0", "https://kinlab.ai"
                ),
                {"kin-model": "0.8.0"},
            )

    def test_event_ahead_of_visible_index_is_preserved(self):
        with mock.patch.object(ucd, "latest_version", return_value="0.7.0"):
            self.assertEqual(
                ucd._resolve_versions(
                    ["kin-model"], "0.8.0", "https://kinlab.ai"
                ),
                {"kin-model": "0.8.0"},
            )


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

    def test_direct_key_with_different_package_is_untouched(self):
        manifest = (
            "[dependencies]\n"
            "kin-model = { package = 'different-package', "
            "version = '=0.6.4', registry = 'kin' }\n"
        )
        path = self.write("Cargo.toml", manifest)
        self.assertFalse(ucd.update_manifest(str(path), "kin-model", "0.7.0"))
        self.assertEqual(path.read_text(encoding="utf-8"), manifest)

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

    def test_inline_alias_field_order_is_irrelevant(self):
        path = self.write(
            "Cargo.toml",
            "[dependencies]\n"
            "model = { registry = 'kin', version = '^0.6.4', "
            "features = ['serde'], package = 'kin-model' }\n",
        )
        self.assertTrue(ucd.update_manifest(str(path), "kin-model", "0.7.0"))
        self.assertIn(
            "registry = 'kin', version = '^0.7.0'",
            path.read_text(encoding="utf-8"),
        )

    def test_inline_version_key_is_selected_at_top_level(self):
        path = self.write(
            "Cargo.toml",
            "[dependencies]\n"
            "model = { package = 'kin-model', metadata = { version = '9.9.9' }, "
            "version = '=0.6.4', registry = 'kin' }\n",
        )
        self.assertTrue(ucd.update_manifest(str(path), "kin-model", "0.7.0"))
        output = path.read_text(encoding="utf-8")
        self.assertIn("metadata = { version = '9.9.9' }", output)
        self.assertIn("version = '=0.7.0', registry", output)

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
        with self.assertRaisesRegex(
            ucd.UpdateError, "physical multiline TOML strings"
        ):
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

    def test_physical_multiline_string_fails_loud_without_writing(self):
        manifest = (
            "[dependencies.kin-model]\n"
            'note = """\n'
            "[not-a-real-table]\n"
            'version = "=9.9.9"\n'
            '"""\n'
            'version = "=0.6.4"\n'
            'registry = "kin"\n'
        )
        path = self.write(
            "Cargo.toml",
            manifest,
        )
        with self.assertRaisesRegex(
            ucd.UpdateError, "physical multiline TOML strings"
        ):
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

    def test_cargo_failure_recreates_deleted_lockfile(self):
        manifest = (
            "[dependencies]\n"
            'kin-model = { version = "=0.6.4", registry = "kin" }\n'
        )
        path = self.write("Cargo.toml", manifest)
        lock = self.write("Cargo.lock", "lock-before\n")
        lock.chmod(0o640)

        def cargo_run(command, check):
            lock.unlink()
            raise subprocess.CalledProcessError(101, command)

        versions = {"kin-model": "0.7.0"}
        plans = ucd.plan_manifests([path], versions)
        with self.assertRaisesRegex(
            ucd.UpdateError, "manifests and Cargo.lock restored"
        ):
            ucd.apply_plans(
                plans, versions, lock_path=lock, cargo_run=cargo_run
            )

        self.assertEqual(path.read_text(encoding="utf-8"), manifest)
        self.assertEqual(lock.read_text(encoding="utf-8"), "lock-before\n")
        self.assertEqual(lock.stat().st_mode & 0o777, 0o640)

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

    def test_duplicate_manifest_arguments_are_deduplicated(self):
        path = self.write(
            "Cargo.toml",
            "[dependencies]\n"
            'kin-model = { version = "=0.6.4", registry = "kin" }\n',
        )
        plans = ucd.plan_manifests(
            [path, path, path.resolve()], {"kin-model": "0.7.0"}
        )
        self.assertEqual(len(plans), 1)


class CliIntegration(TemporaryManifestTest):
    script = Path(__file__).resolve().parent / "update-cargo-registry-deps.py"

    def run_cli(self, *arguments, env=None):
        return subprocess.run(
            [sys.executable, str(self.script), *arguments],
            cwd=self.root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_dry_run_cli_reports_without_mutating_manifest_or_lock(self):
        manifest = (
            "[dependencies]\n"
            "model={registry='kin',package='kin-model',version='=0.6.4'}\n"
        )
        path = self.write("Cargo.toml", manifest)
        lock = self.write("Cargo.lock", "lock-before\n")

        result = self.run_cli(
            "--crate",
            "kin-model",
            "--version",
            "0.7.0",
            "--dry-run",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[dry-run] would update", result.stdout)
        self.assertEqual(path.read_text(encoding="utf-8"), manifest)
        self.assertEqual(lock.read_text(encoding="utf-8"), "lock-before\n")

    def test_cli_preflight_failure_leaves_all_manifests_unchanged(self):
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

        result = self.run_cli(
            "--crate",
            "kin-model",
            "--version",
            "0.7.0",
            "--manifest",
            "Cargo.toml",
            "--manifest",
            "crates/member/Cargo.toml",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("dependency wave aborted", result.stderr)
        self.assertEqual(first.read_text(encoding="utf-8"), first_text)
        self.assertEqual(second.read_text(encoding="utf-8"), second_text)

    def test_cli_cargo_failure_rolls_back_and_exits_nonzero(self):
        manifest = (
            "[dependencies]\n"
            'kin-model = { version = "=0.6.4", registry = "kin" }\n'
        )
        path = self.write("Cargo.toml", manifest)
        lock = self.write("Cargo.lock", "lock-before\n")
        fake_cargo = self.write(
            "bin/cargo",
            "#!/bin/sh\n"
            "python3 -c 'from pathlib import Path; Path(\"Cargo.lock\").unlink()'\n"
            "exit 101\n",
        )
        fake_cargo.chmod(0o755)
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_cargo.parent}{os.pathsep}{environment['PATH']}"

        result = self.run_cli(
            "--crate",
            "kin-model",
            "--version",
            "0.7.0",
            env=environment,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("manifests and Cargo.lock restored", result.stderr)
        self.assertEqual(path.read_text(encoding="utf-8"), manifest)
        self.assertEqual(lock.read_text(encoding="utf-8"), "lock-before\n")

    def test_cli_no_change_uses_exit_two(self):
        self.write(
            "Cargo.toml",
            "[dependencies]\n"
            'kin-model = { version = "=0.7.0", registry = "kin" }\n',
        )
        result = self.run_cli(
            "--crate", "kin-model", "--version", "0.7.0"
        )
        self.assertEqual(result.returncode, 2, result.stderr)


class WorkflowContract(unittest.TestCase):
    def test_no_crates_is_a_no_change_exit(self):
        self.assertEqual(ucd.main([]), 2)

    def test_updater_failure_aborts_and_refresh_is_single_transaction(self):
        workflow_path = (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/cargo-dependency-wave.yml"
        )
        workflow = workflow_path.read_text(encoding="utf-8")

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

    def test_exact_workflow_function_propagates_exit_status(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/cargo-dependency-wave.yml"
        ).read_text(encoding="utf-8")
        lines = workflow.splitlines()
        start = next(
            index for index, line in enumerate(lines) if line.strip() == "run_update() {"
        )
        end = next(
            index
            for index in range(start + 1, len(lines))
            if lines[index].strip() == "}"
        )
        function = textwrap.dedent("\n".join(lines[start : end + 1]))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper = root / ".kin-actions/scripts/update-cargo-registry-deps.py"
            helper.parent.mkdir(parents=True)
            helper.write_text(
                "import os\nraise SystemExit(int(os.environ['FAKE_UPDATE_RC']))\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()

            for status in (0, 2):
                with self.subTest(status=status):
                    environment["FAKE_UPDATE_RC"] = str(status)
                    result = subprocess.run(
                        [
                            "bash",
                            "-c",
                            function
                            + "\nset +e\nrun_update\n"
                            + 'code=$?\nset -e\nprintf "code=%s\\n" "$code"\n',
                        ],
                        cwd=root,
                        env=environment,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(f"code={status}", result.stdout)

            environment["FAKE_UPDATE_RC"] = "1"
            failed = subprocess.run(
                ["bash", "-c", function + "\nset +e\nrun_update\n"],
                cwd=root,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(failed.returncode, 1)
            self.assertIn("aborting without a PR", failed.stdout)


if __name__ == "__main__":
    unittest.main()
