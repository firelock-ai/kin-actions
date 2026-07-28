#!/usr/bin/env python3
"""Unit tests for the Kin registry version-bump gate.

Run with: ``python3 -m unittest discover -s scripts -p 'test_*.py'``

These exercise the pure classifier (:func:`classify_path`), the manifest
dependency-change detector (:func:`manifest_deps_changed`) and the gate
decision (:func:`evaluate_gate`) with no git / cargo / network access. The two
headline cases the gate must get right are covered explicitly:

  * a docs-only PR passes WITHOUT a version bump
    (``EvaluateGate.test_docs_only_pr_passes_without_bump``), and
  * a crate-``src`` PR still REQUIRES one
    (``EvaluateGate.test_crate_src_change_without_bump_fails``).
"""
import importlib.util
import io
import json
import sys
import urllib.error
import unittest
from pathlib import Path
from unittest import mock


def _load(name, filename):
    path = Path(__file__).resolve().parent / filename
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cvb = _load("check_version_bump", "check-version-bump.py")
CHECKSUM = "0" * 64


def registry_row(
    *,
    name="x",
    version="1.0.0",
    yanked=False,
    omit=(),
):
    row = {
        "name": name,
        "vers": version,
        "yanked": yanked,
        "cksum": CHECKSUM,
        "deps": [],
        "features": {},
    }
    for key in omit:
        row.pop(key)
    return (json.dumps(row, separators=(",", ":")) + "\n").encode()


class ClassifyPath(unittest.TestCase):
    def c(self, path):
        return cvb.classify_path(path, [])

    def test_release_source_paths(self):
        for path in [
            "src/main.rs",
            "src/lib.rs",
            "crates/foo/src/lib.rs",
            "crates/foo/src/nested/mod.rs",
            "packages/bar/src/index.rs",
            "build.rs",
            "crates/foo/build.rs",
        ]:
            self.assertEqual(self.c(path), "source", path)

    def test_manifest_paths(self):
        self.assertEqual(self.c("Cargo.toml"), "manifest")
        self.assertEqual(self.c("crates/foo/Cargo.toml"), "manifest")

    def test_non_release_paths_are_ignored(self):
        for path in [
            "README.md",
            "docs/guide.md",
            "crates/foo/README.md",
            "crates/foo/CHANGELOG.md",
            ".github/workflows/ci.yml",
            ".cargo/config.toml",
            "crates/foo/tests/integration.rs",
            "tests/it.rs",
            "benches/bench.rs",
            "examples/demo.rs",
            "fuzz/fuzz_targets/a.rs",
            ".gitignore",
            "LICENSE",
        ]:
            self.assertEqual(self.c(path), "ignore", path)

    def test_crate_named_test_under_src_is_still_source(self):
        # A crate literally named `test`/`docs` must not be mistaken for a
        # test/doc directory when it has a real src tree.
        self.assertEqual(self.c("crates/test/src/lib.rs"), "source")
        self.assertEqual(self.c("crates/docs/src/lib.rs"), "source")

    def test_markdown_under_src_is_ignored(self):
        self.assertEqual(self.c("crates/foo/src/README.md"), "ignore")

    def test_extra_source_root(self):
        self.assertEqual(cvb.classify_path("runtime/engine.rs", ["runtime"]), "source")
        self.assertEqual(cvb.classify_path("runtime/notes.md", ["runtime"]), "ignore")


class ManifestDepsChanged(unittest.TestCase):
    def test_dependency_value_change_detected(self):
        base = '[package]\nname = "x"\nversion = "0.1.0"\n[dependencies]\nserde = "1.0"\n'
        head = '[package]\nname = "x"\nversion = "0.1.0"\n[dependencies]\nserde = "1.1"\n'
        self.assertTrue(cvb.manifest_deps_changed(base, head))

    def test_version_only_change_is_not_a_dep_change(self):
        base = '[package]\nname = "x"\nversion = "0.1.0"\n[dependencies]\nserde = "1.0"\n'
        head = '[package]\nname = "x"\nversion = "0.2.0"\n[dependencies]\nserde = "1.0"\n'
        self.assertFalse(cvb.manifest_deps_changed(base, head))

    def test_metadata_change_is_not_a_dep_change(self):
        base = '[package]\nname = "x"\ndescription = "a"\n[dependencies]\nserde = "1.0"\n'
        head = '[package]\nname = "x"\ndescription = "b"\n[dependencies]\nserde = "1.0"\n'
        self.assertFalse(cvb.manifest_deps_changed(base, head))

    def test_dev_dependencies_change_is_ignored(self):
        base = '[dependencies]\nserde = "1.0"\n[dev-dependencies]\ntempfile = "3"\n'
        head = '[dependencies]\nserde = "1.0"\n[dev-dependencies]\ntempfile = "4"\n'
        self.assertFalse(cvb.manifest_deps_changed(base, head))

    def test_feature_change_detected(self):
        base = '[features]\ndefault = []\n'
        head = '[features]\ndefault = ["x"]\n'
        self.assertTrue(cvb.manifest_deps_changed(base, head))

    def test_target_dependency_change_detected(self):
        base = '[target.\'cfg(unix)\'.dependencies]\nlibc = "0.2"\n'
        head = '[target.\'cfg(unix)\'.dependencies]\nlibc = "0.3"\n'
        self.assertTrue(cvb.manifest_deps_changed(base, head))

    def test_comment_only_change_is_ignored(self):
        base = '[dependencies]\nserde = "1.0"\n'
        head = '[dependencies]\n# pin to 1.0 for MSRV\nserde = "1.0"\n'
        self.assertFalse(cvb.manifest_deps_changed(base, head))

    def test_new_manifest_is_release_relevant(self):
        self.assertTrue(cvb.manifest_deps_changed(None, '[dependencies]\nserde = "1"\n'))


class EvaluateGate(unittest.TestCase):
    def gate(self, **kwargs):
        defaults = dict(
            package="x",
            version="0.1.0",
            base_version="0.1.0",
            published=["0.1.0"],
            source_changes=[],
            dep_manifest_changes=[],
            release_label=False,
        )
        defaults.update(kwargs)
        return cvb.evaluate_gate(**defaults)

    def test_docs_only_pr_passes_without_bump(self):
        failures, require_bump, relevant = self.gate()
        self.assertFalse(require_bump)
        self.assertEqual(failures, [])
        self.assertEqual(relevant, [])

    def test_crate_src_change_without_bump_fails(self):
        failures, require_bump, _ = self.gate(source_changes=["crates/x/src/lib.rs"])
        self.assertTrue(require_bump)
        self.assertTrue(any("stayed at" in m for m in failures))

    def test_crate_src_change_with_bump_passes(self):
        failures, require_bump, _ = self.gate(
            version="0.2.0", source_changes=["crates/x/src/lib.rs"]
        )
        self.assertTrue(require_bump)
        self.assertEqual(failures, [])

    def test_dep_manifest_change_without_bump_fails(self):
        failures, require_bump, _ = self.gate(dep_manifest_changes=["Cargo.toml"])
        self.assertTrue(require_bump)
        self.assertTrue(failures)

    def test_release_label_forces_bump(self):
        failures, require_bump, _ = self.gate(release_label=True)
        self.assertTrue(require_bump)
        self.assertTrue(any("release label" in m for m in failures))

    def test_already_published_version_with_src_change_fails(self):
        failures, _, _ = self.gate(
            version="0.2.0",
            published=["0.1.0", "0.2.0"],
            source_changes=["src/lib.rs"],
        )
        self.assertTrue(any("already published" in m for m in failures))

    def test_version_only_move_to_published_version_fails(self):
        failures, require_bump, _ = self.gate(
            version="1.1.0",
            base_version="1.0.0",
            published=["1.1.0"],
        )
        self.assertFalse(require_bump)
        self.assertTrue(any("already published and immutable" in m for m in failures))

    def test_initial_version_colliding_with_registry_fails(self):
        failures, _, _ = self.gate(
            version="1.1.0",
            base_version=None,
            published=["1.1.0"],
        )
        self.assertTrue(any("already published and immutable" in m for m in failures))

    def test_yanked_version_remains_an_immutable_collision(self):
        failures, _, _ = self.gate(
            version="1.1.0",
            base_version="1.0.0",
            published=["1.1.0"],
        )
        self.assertTrue(any("already published and immutable" in m for m in failures))

    def test_highest_yanked_version_still_sets_monotonic_floor(self):
        failures, _, _ = self.gate(
            version="1.5.0",
            base_version="1.0.0",
            published=["2.0.0"],
        )
        self.assertTrue(any("lower than newest published 2.0.0" in m for m in failures))

    def test_version_below_newest_published_always_fails(self):
        failures, _, _ = self.gate(version="0.1.0", published=["0.2.0"])
        self.assertTrue(any("lower than newest published" in m for m in failures))

    def test_version_movement_must_be_strictly_forward_from_base(self):
        failures, _, _ = self.gate(
            version="0.1.0",
            base_version="0.2.0",
            published=[],
        )
        self.assertTrue(any("strictly forward from base" in m for m in failures))

    def test_version_only_downgrade_fails(self):
        failures, require_bump, _ = self.gate(
            version="1.9.9",
            base_version="2.0.0",
            published=[],
        )
        self.assertFalse(require_bump)
        self.assertTrue(any("strictly forward from base" in m for m in failures))

    def test_unpublished_higher_base_still_prevents_downgrade(self):
        failures, _, _ = self.gate(
            version="1.5.0",
            base_version="2.0.0",
            published=["1.0.0"],
            source_changes=["src/lib.rs"],
        )
        self.assertTrue(any("strictly forward from base" in m for m in failures))

    def test_stable_to_same_core_prerelease_is_not_forward(self):
        failures, _, _ = self.gate(
            version="1.0.0-rc.1",
            base_version="1.0.0",
            published=[],
            source_changes=["src/lib.rs"],
        )
        self.assertTrue(any("strictly forward from base" in m for m in failures))

    def test_prerelease_to_stable_is_forward(self):
        failures, _, _ = self.gate(
            version="1.0.0",
            base_version="1.0.0-rc.1",
            published=[],
            source_changes=["src/lib.rs"],
        )
        self.assertEqual(failures, [])

    def test_prerelease_identifiers_follow_semver_order(self):
        ordered = (
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-alpha.beta",
            "1.0.0-beta",
            "1.0.0-beta.2",
            "1.0.0-beta.11",
            "1.0.0-rc.1",
            "1.0.0",
        )
        self.assertEqual(sorted(ordered, key=cvb.parse_version), list(ordered))

    def test_build_metadata_only_change_is_not_forward(self):
        failures, _, _ = self.gate(
            version="1.0.0+build.2",
            base_version="1.0.0+build.1",
            published=[],
        )
        self.assertTrue(any("strictly forward from base" in m for m in failures))

    def test_no_base_version_skips_bump_comparison(self):
        # First commit / unresolved base: cannot compare, must not crash.
        failures, require_bump, _ = self.gate(
            base_version=None, source_changes=["src/lib.rs"], published=[]
        )
        self.assertTrue(require_bump)
        self.assertEqual(failures, [])


class Labels(unittest.TestCase):
    def test_release_labels_detected(self):
        self.assertTrue(cvb.has_release_label(cvb.parse_labels("chore, release")))
        self.assertTrue(cvb.has_release_label(cvb.parse_labels("release:minor")))
        self.assertTrue(cvb.has_release_label(cvb.parse_labels("release/patch")))

    def test_non_release_labels_ignored(self):
        self.assertFalse(cvb.has_release_label(cvb.parse_labels("chore docs tests")))
        self.assertFalse(cvb.has_release_label(cvb.parse_labels("")))


class ReleaseCandidate(unittest.TestCase):
    def test_docs_only_followup_with_same_version_is_not_a_release(self):
        self.assertFalse(cvb.is_release_candidate("1.2.3", "1.2.3"))

    def test_version_movement_owns_release_authority(self):
        self.assertTrue(cvb.is_release_candidate("1.2.4", "1.2.3"))

    def test_downgrade_does_not_own_release_authority(self):
        self.assertFalse(cvb.is_release_candidate("1.2.2", "1.2.3"))

    def test_stable_to_prerelease_does_not_own_release_authority(self):
        self.assertFalse(cvb.is_release_candidate("1.2.3-rc.1", "1.2.3"))

    def test_prerelease_to_stable_owns_release_authority(self):
        self.assertTrue(cvb.is_release_candidate("1.2.3", "1.2.3-rc.1"))

    def test_build_metadata_only_change_does_not_own_release_authority(self):
        self.assertFalse(
            cvb.is_release_candidate("1.2.3+build.2", "1.2.3+build.1")
        )

    def test_first_commit_is_a_release_candidate(self):
        self.assertTrue(cvb.is_release_candidate("1.2.3", None))

    def test_inherited_base_version_resolves_from_workspace_root(self):
        member = (
            '[package]\nname = "x"\nversion.workspace = true\n'
            '[dependencies.dep]\nversion = "9.9.9"\n'
        )
        root = '[workspace.package]\nversion = "1.2.3"\n'

        def show(_ref, path):
            return root if path == "Cargo.toml" else member

        original = cvb.git_show_file
        try:
            cvb.git_show_file = show
            self.assertEqual(
                cvb.base_manifest_version("HEAD^", "crates/x/Cargo.toml"),
                "1.2.3",
            )
        finally:
            cvb.git_show_file = original

    def test_inherited_base_never_falls_back_to_dependency_version(self):
        member = (
            '[package]\nname = "x"\nversion.workspace = true\n'
            '[dependencies.dep]\nversion = "9.9.9"\n'
        )

        def show(_ref, path):
            return '[workspace]\nmembers = ["crates/x"]\n' if path == "Cargo.toml" else member

        with mock.patch.object(cvb, "git_show_file", side_effect=show):
            with self.assertRaisesRegex(
                SystemExit, "workspace.package.*version"
            ):
                cvb.base_manifest_version("HEAD^", "crates/x/Cargo.toml")

    def test_multi_commit_branch_push_uses_event_before(self):
        before = "a" * 40
        with mock.patch.object(cvb, "_commit_available", return_value=True):
            self.assertEqual(cvb.select_base_ref("", before, "branch"), before)

    def test_tag_push_does_not_trust_event_before(self):
        before = "a" * 40
        with mock.patch.object(cvb, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(cvb.select_base_ref("", before, "tag"), "HEAD^")

    def test_zero_before_sha_uses_no_base_even_when_head_has_parent(self):
        with mock.patch.object(cvb, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(cvb.select_base_ref("", "0" * 40, "branch"), "")
        run.assert_not_called()

    def test_malformed_before_sha_fails_closed(self):
        with self.assertRaisesRegex(SystemExit, "invalid push before SHA"):
            cvb.select_base_ref("", "not-a-sha", "branch")

    def test_missing_before_commit_fails_closed_after_fetch(self):
        before = "a" * 40
        with mock.patch.object(cvb, "_commit_available", return_value=False), mock.patch.object(
            cvb, "run"
        ) as run:
            with self.assertRaisesRegex(SystemExit, "refusing to infer"):
                cvb.select_base_ref("", before, "branch")
        run.assert_called_once_with(
            ["git", "fetch", "--no-tags", "--depth=1", "origin", before],
            check=False,
        )

    def test_missing_before_commit_is_recovered_by_fetch(self):
        before = "a" * 40
        with mock.patch.object(
            cvb, "_commit_available", side_effect=[False, True]
        ), mock.patch.object(cvb, "run") as run:
            self.assertEqual(cvb.select_base_ref("", before, "branch"), before)
        run.assert_called_once_with(
            ["git", "fetch", "--no-tags", "--depth=1", "origin", before],
            check=False,
        )


class RegistryVersions(unittest.TestCase):
    def response(self, body):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = body
        return response

    def test_valid_404_is_the_only_empty_registry(self):
        error = urllib.error.HTTPError(
            "https://kinlab.ai/registry/cargo/1/x", 404, "Not Found", {}, io.BytesIO()
        )
        self.addCleanup(error.close)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            self.assertEqual(cvb.published_versions("https://kinlab.ai", "x"), [])

    def test_non_404_http_error_fails_closed(self):
        error = urllib.error.HTTPError(
            "https://kinlab.ai/registry/cargo/1/x", 500, "Error", {}, io.BytesIO()
        )
        self.addCleanup(error.close)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(SystemExit, "HTTP 500"):
                cvb.published_versions("https://kinlab.ai", "x")

    def test_io_error_fails_closed(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            with self.assertRaisesRegex(SystemExit, "offline"):
                cvb.published_versions("https://kinlab.ai", "x")

    def test_invalid_json_row_fails_closed(self):
        with mock.patch(
            "urllib.request.urlopen", return_value=self.response(b"not json\n")
        ):
            with self.assertRaisesRegex(SystemExit, "invalid JSON"):
                cvb.published_versions("https://kinlab.ai", "x")

    def test_non_object_json_row_fails_closed(self):
        with mock.patch(
            "urllib.request.urlopen", return_value=self.response(b'["x"]\n')
        ):
            with self.assertRaisesRegex(SystemExit, "expected an object"):
                cvb.published_versions("https://kinlab.ai", "x")

    def test_empty_successful_response_fails_closed(self):
        with mock.patch(
            "urllib.request.urlopen", return_value=self.response(b"\n \n")
        ):
            with self.assertRaisesRegex(SystemExit, "empty successful response"):
                cvb.published_versions("https://kinlab.ai", "x")

    def test_missing_name_row_fails_closed(self):
        with mock.patch(
            "urllib.request.urlopen",
            return_value=self.response(registry_row(omit=("name",))),
        ):
            with self.assertRaisesRegex(SystemExit, "missing string 'name'"):
                cvb.published_versions("https://kinlab.ai", "x")

    def test_mismatched_name_row_fails_closed(self):
        with mock.patch(
            "urllib.request.urlopen",
            return_value=self.response(registry_row(name="y")),
        ):
            with self.assertRaisesRegex(SystemExit, "does not match"):
                cvb.published_versions("https://kinlab.ai", "x")

    def test_missing_version_row_fails_closed(self):
        with mock.patch(
            "urllib.request.urlopen",
            return_value=self.response(registry_row(omit=("vers",))),
        ):
            with self.assertRaisesRegex(SystemExit, "missing string 'vers'"):
                cvb.published_versions("https://kinlab.ai", "x")

    def test_invalid_semver_row_fails_closed(self):
        with mock.patch(
            "urllib.request.urlopen",
            return_value=self.response(registry_row(version="latest")),
        ):
            with self.assertRaisesRegex(SystemExit, "invalid SemVer"):
                cvb.published_versions("https://kinlab.ai", "x")

    def test_missing_yanked_row_fails_closed(self):
        with mock.patch(
            "urllib.request.urlopen",
            return_value=self.response(registry_row(omit=("yanked",))),
        ):
            with self.assertRaisesRegex(SystemExit, "missing boolean 'yanked'"):
                cvb.published_versions("https://kinlab.ai", "x")

    def test_valid_rows_preserve_yanked_versions_in_immutable_history(self):
        body = b"".join(
            (
                registry_row(version="1.0.0-rc.1"),
                registry_row(version="1.0.0"),
                registry_row(version="2.0.0", yanked=True),
            )
        )
        with mock.patch(
            "urllib.request.urlopen", return_value=self.response(body)
        ):
            self.assertEqual(
                cvb.published_versions("https://kinlab.ai", "x"),
                ["1.0.0-rc.1", "1.0.0", "2.0.0"],
            )


if __name__ == "__main__":
    unittest.main()
