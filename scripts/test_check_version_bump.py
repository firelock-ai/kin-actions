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
import unittest
from pathlib import Path


def _load(name, filename):
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cvb = _load("check_version_bump", "check-version-bump.py")


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

    def test_version_below_newest_published_always_fails(self):
        failures, _, _ = self.gate(version="0.1.0", published=["0.2.0"])
        self.assertTrue(any("lower than newest published" in m for m in failures))

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


if __name__ == "__main__":
    unittest.main()
