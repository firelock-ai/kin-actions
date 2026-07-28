#!/usr/bin/env python3
"""Unit tests for the Kin registry dependency-wave manifest updater.

Run with: ``python3 -m unittest discover -s scripts -p 'test_*.py'``

These exercise the pure manifest rewriter (:func:`update_manifest`) with no
network / cargo access. The headline behaviours the dependency wave relies on
are covered explicitly:

  * a real run rewrites a ``registry = "kin"`` pin in place, and
  * the existing Cargo requirement operator is preserved exactly, and
  * ``--dry-run`` reports the same change WITHOUT touching the file, so the
    report-only mode never mutates a manifest.
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path


def _load(name, filename):
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ucd = _load("update_cargo_registry_deps", "update-cargo-registry-deps.py")

KIN_MANIFEST = """\
[dependencies]
kin-db = { version = "0.2.24", registry = "kin" }
serde = { version = "1", features = ["derive"] }
"""


class UpdateManifest(unittest.TestCase):
    def _write(self, text):
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".toml", delete=False, encoding="utf-8"
        )
        tmp.write(text)
        tmp.close()
        return Path(tmp.name)

    def test_real_run_rewrites_kin_pin(self):
        path = self._write(KIN_MANIFEST)
        try:
            changed = ucd.update_manifest(str(path), "kin-db", "0.2.25")
            self.assertTrue(changed)
            out = path.read_text(encoding="utf-8")
            self.assertIn('kin-db = { version = "0.2.25", registry = "kin" }', out)
        finally:
            path.unlink()

    def test_dry_run_reports_without_writing(self):
        path = self._write(KIN_MANIFEST)
        try:
            changed = ucd.update_manifest(
                str(path), "kin-db", "0.2.25", dry_run=True
            )
            self.assertTrue(changed)
            # The file must be byte-for-byte unchanged under --dry-run.
            self.assertEqual(path.read_text(encoding="utf-8"), KIN_MANIFEST)
        finally:
            path.unlink()

    def test_non_kin_dep_is_untouched(self):
        path = self._write(KIN_MANIFEST)
        try:
            # serde carries no `registry = "kin"`, so it is never rewritten.
            changed = ucd.update_manifest(str(path), "serde", "2")
            self.assertFalse(changed)
            self.assertEqual(path.read_text(encoding="utf-8"), KIN_MANIFEST)
        finally:
            path.unlink()

    def test_already_current_pin_is_noop(self):
        path = self._write(KIN_MANIFEST)
        try:
            changed = ucd.update_manifest(str(path), "kin-db", "0.2.24")
            self.assertFalse(changed)
        finally:
            path.unlink()

    def test_exact_pin_remains_exact(self):
        path = self._write(
            '[dependencies]\nkin-model = { version = "=0.6.4", registry = "kin" }\n'
        )
        try:
            changed = ucd.update_manifest(str(path), "kin-model", "0.7.0")
            self.assertTrue(changed)
            self.assertIn(
                'kin-model = { version = "=0.7.0", registry = "kin" }',
                path.read_text(encoding="utf-8"),
            )
        finally:
            path.unlink()

    def test_explicit_caret_and_tilde_operators_are_preserved(self):
        for requirement, expected in (
            ("^0.6.4", "^0.7.0"),
            ("~0.6.4", "~0.7.0"),
            ("= 0.6.4", "= 0.7.0"),
        ):
            with self.subTest(requirement=requirement):
                path = self._write(
                    "[dependencies]\n"
                    f'kin-model = {{ version = "{requirement}", registry = "kin" }}\n'
                )
                try:
                    changed = ucd.update_manifest(str(path), "kin-model", "0.7.0")
                    self.assertTrue(changed)
                    self.assertIn(
                        f'version = "{expected}"',
                        path.read_text(encoding="utf-8"),
                    )
                finally:
                    path.unlink()

    def test_package_alias_preserves_exact_pin(self):
        path = self._write(
            "[dependencies]\n"
            'model = { package = "kin-model", version = "=0.6.4", '
            'registry = "kin" }\n'
        )
        try:
            changed = ucd.update_manifest(str(path), "kin-model", "0.7.0")
            self.assertTrue(changed)
            self.assertIn('version = "=0.7.0"', path.read_text(encoding="utf-8"))
        finally:
            path.unlink()

    def test_complex_requirement_fails_loud_without_writing(self):
        manifest = (
            "[dependencies]\n"
            'kin-model = { version = ">=0.6, <0.7", registry = "kin" }\n'
        )
        path = self._write(manifest)
        try:
            with self.assertRaisesRegex(ValueError, "unsupported Cargo requirement"):
                ucd.update_manifest(str(path), "kin-model", "0.7.0")
            self.assertEqual(path.read_text(encoding="utf-8"), manifest)
        finally:
            path.unlink()


if __name__ == "__main__":
    unittest.main()
