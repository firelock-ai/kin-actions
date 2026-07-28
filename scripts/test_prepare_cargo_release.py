"""Adversarial unit tests for automatic Cargo release preparation."""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("prepare-cargo-release.py")
SPEC = importlib.util.spec_from_file_location("prepare_cargo_release", SCRIPT)
pcr = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = pcr
SPEC.loader.exec_module(pcr)


def resolver(_root: Path, _package: str, expected: str, _tracked: bool) -> None:
    if not pcr.SEMVER_RE.fullmatch(expected):
        raise AssertionError(expected)


class StableVersionTests(unittest.TestCase):
    def test_standard_successors(self) -> None:
        version = pcr.StableVersion.parse("0.9.9")
        self.assertEqual(str(version.bump("patch")), "0.9.10")
        self.assertEqual(str(version.bump("minor")), "0.10.0")
        self.assertEqual(str(version.bump("major")), "1.0.0")

    def test_prerelease_build_and_leading_zero_fail(self) -> None:
        for raw in ("1.2.3-rc.1", "1.2.3+build", "01.2.3", "v1.2.3", "1.2"):
            with self.subTest(raw=raw), self.assertRaises(
                pcr.ReleasePreparationError
            ):
                pcr.StableVersion.parse(raw)


class PrepareReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def prepare(
        self,
        *,
        intent: str = "patch",
        base: str = "0.1.0",
        manifest: str = "Cargo.toml",
        tracked_lock: bool = False,
        custom_resolver=resolver,
    ):
        return pcr.prepare_release(
            root=self.root,
            package="kin-fixture",
            manifest=manifest,
            base_version=base,
            intent=intent,
            tracked=lambda _root, path: tracked_lock and path == "Cargo.lock",
            resolver=custom_resolver,
        )

    def test_direct_package_patch_changes_only_authority(self) -> None:
        manifest = self.write(
            "Cargo.toml",
            '[package]\nname = "kin-fixture"\nversion = "0.1.0"\n',
        )
        result = self.prepare()
        self.assertIn('version = "0.1.1"', manifest.read_text())
        self.assertEqual(result["generated_paths"], ["Cargo.toml"])
        self.assertEqual(result["target_version"], "0.1.1")

    def test_workspace_inheritance_updates_root_authority(self) -> None:
        root = self.write(
            "Cargo.toml",
            '[workspace]\nmembers = ["crates/x"]\n'
            '[workspace.package]\nversion = "0.4.9"\n',
        )
        member = self.write(
            "crates/x/Cargo.toml",
            '[package]\nname = "kin-fixture"\nversion.workspace = true\n',
        )
        result = self.prepare(
            manifest="crates/x/Cargo.toml", base="0.4.9", intent="minor"
        )
        self.assertIn('version = "0.5.0"', root.read_text())
        self.assertNotIn('version = "', member.read_text())
        self.assertEqual(result["allowed_paths"], ["Cargo.toml"])

    def test_workspace_inheritance_ignores_dependency_version_keys(self) -> None:
        self.write(
            "Cargo.toml",
            '[workspace.package]\nversion = "1.2.3"\n',
        )
        self.write(
            "crates/x/Cargo.toml",
            '[package]\nname = "kin-fixture"\nversion.workspace = true\n'
            '[dependencies.dep]\nversion = "9.9.9"\n',
        )
        authority = pcr.find_version_authority(
            self.root, "crates/x/Cargo.toml"
        )
        self.assertEqual(str(authority.version), "1.2.3")
        self.assertEqual(authority.path, (self.root / "Cargo.toml").resolve())

    def test_workspace_marker_outside_package_is_rejected(self) -> None:
        self.write(
            "Cargo.toml",
            '[workspace.package]\nversion = "1.2.3"\n',
        )
        self.write(
            "crates/x/Cargo.toml",
            '[package]\nname = "kin-fixture"\n'
            '[package.metadata]\nversion.workspace = true\n',
        )
        with self.assertRaisesRegex(
            pcr.ReleasePreparationError, "neither a direct version"
        ):
            pcr.find_version_authority(self.root, "crates/x/Cargo.toml")

    def test_tracked_lock_updates_only_sourceless_local_kin_records(self) -> None:
        self.write(
            "Cargo.toml",
            '[package]\nname = "kin-fixture"\nversion = "1.2.3"\n',
        )
        lock = self.write(
            "Cargo.lock",
            """version = 4

[[package]]
name = "kin-fixture"
version = "1.2.3"

[[package]]
name = "kin-local-helper"
version = "1.2.3"

[[package]]
name = "kin-registry"
version = "1.2.3"
source = "registry+https://example.invalid/index"
checksum = "abc123"

[[package]]
name = "serde"
version = "1.2.3"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "def456"
""",
        )
        result = self.prepare(base="1.2.3", tracked_lock=True)
        text = lock.read_text()
        self.assertEqual(text.count('version = "1.2.4"'), 2)
        self.assertIn('name = "kin-registry"\nversion = "1.2.3"', text)
        self.assertIn('checksum = "abc123"', text)
        self.assertIn('name = "serde"\nversion = "1.2.3"', text)
        self.assertEqual(result["allowed_paths"], ["Cargo.lock", "Cargo.toml"])

    def test_untracked_lock_is_refused_and_manifest_is_untouched(self) -> None:
        manifest = self.write(
            "Cargo.toml",
            '[package]\nname = "kin-fixture"\nversion = "0.1.0"\n',
        )
        self.write("Cargo.lock", "version = 4\n")
        before = manifest.read_bytes()
        with self.assertRaisesRegex(
            pcr.ReleasePreparationError, "not tracked"
        ):
            self.prepare()
        self.assertEqual(manifest.read_bytes(), before)

    def test_missing_lock_is_not_created(self) -> None:
        self.write(
            "Cargo.toml",
            '[package]\nname = "kin-fixture"\nversion = "0.1.0"\n',
        )
        self.prepare()
        self.assertFalse((self.root / "Cargo.lock").exists())

    def test_idempotent_target_and_escalation_never_downgrade(self) -> None:
        manifest = self.write(
            "Cargo.toml",
            '[package]\nname = "kin-fixture"\nversion = "0.2.0"\n',
        )
        minor = self.prepare(base="0.1.0", intent="patch")
        self.assertEqual(minor["target_version"], "0.2.0")
        self.assertEqual(minor["generated_paths"], [])
        major = self.prepare(base="0.1.0", intent="major")
        self.assertEqual(major["target_version"], "1.0.0")
        self.assertIn('version = "1.0.0"', manifest.read_text())

    def test_arbitrary_existing_generated_version_fails_closed(self) -> None:
        manifest = self.write(
            "Cargo.toml",
            '[package]\nname = "kin-fixture"\nversion = "0.1.7"\n',
        )
        before = manifest.read_bytes()
        with self.assertRaisesRegex(
            pcr.ReleasePreparationError, "automatic successors"
        ):
            self.prepare(base="0.1.0")
        self.assertEqual(manifest.read_bytes(), before)

    def test_metadata_failure_rolls_back_manifest_and_lock(self) -> None:
        manifest = self.write(
            "Cargo.toml",
            '[package]\nname = "kin-fixture"\nversion = "0.1.0"\n',
        )
        lock = self.write(
            "Cargo.lock",
            'version = 4\n\n[[package]]\nname = "kin-fixture"\nversion = "0.1.0"\n',
        )
        before_manifest = manifest.read_bytes()
        before_lock = lock.read_bytes()

        def fail(*_args):
            raise pcr.ReleasePreparationError("metadata rejected fixture")

        with self.assertRaisesRegex(
            pcr.ReleasePreparationError, "metadata rejected"
        ):
            self.prepare(tracked_lock=True, custom_resolver=fail)
        self.assertEqual(manifest.read_bytes(), before_manifest)
        self.assertEqual(lock.read_bytes(), before_lock)

    def test_symlink_authority_fails_closed(self) -> None:
        target = self.write(
            "actual.toml",
            '[package]\nname = "kin-fixture"\nversion = "0.1.0"\n',
        )
        os.symlink(target, self.root / "Cargo.toml")
        with self.assertRaisesRegex(pcr.ReleasePreparationError, "symlink"):
            self.prepare()

    def test_file_mode_is_preserved(self) -> None:
        manifest = self.write(
            "Cargo.toml",
            '[package]\nname = "kin-fixture"\nversion = "0.1.0"\n',
        )
        manifest.chmod(0o640)
        self.prepare()
        self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o640)

    def test_inspection_resolves_exact_authority_and_tracked_lock(self) -> None:
        self.write(
            "Cargo.toml",
            '[workspace]\nmembers = ["crates/x"]\n'
            '[workspace.package]\nversion = "0.4.9"\n',
        )
        self.write(
            "crates/x/Cargo.toml",
            '[package]\nname = "kin-fixture"\nversion.workspace = true\n',
        )
        self.write("Cargo.lock", "version = 4\n")
        result = pcr.inspect_release_inputs(
            root=self.root,
            manifest="crates/x/Cargo.toml",
            tracked=lambda _root, path: path == "Cargo.lock",
        )
        self.assertEqual(result["authority_path"], "Cargo.toml")
        self.assertEqual(result["current_version"], "0.4.9")
        self.assertEqual(result["allowed_paths"], ["Cargo.lock", "Cargo.toml"])

    def test_version_at_ref_uses_exact_package_or_workspace_blob(self) -> None:
        blobs = {
            "tag:crates/x/Cargo.toml": (
                '[package]\nname = "x"\nversion.workspace = true\n'
                '[dependencies.dep]\nversion = "9.9.9"\n'
            ),
            "tag:Cargo.toml": (
                '[workspace.package]\nversion = "1.2.3"\n'
            ),
        }

        def run(args, **_kwargs):
            result = mock.MagicMock()
            key = args[-1]
            result.returncode = 0 if key in blobs else 1
            result.stdout = blobs.get(key, "")
            return result

        with mock.patch.object(pcr.subprocess, "run", side_effect=run):
            self.assertEqual(
                pcr.version_at_ref(
                    self.root, "tag", "crates/x/Cargo.toml"
                ),
                "1.2.3",
            )


if __name__ == "__main__":
    unittest.main()
