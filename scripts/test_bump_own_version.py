#!/usr/bin/env python3
"""Tests for shared SemVer authority in ``bump-own-version.py``."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


def _load():
    path = Path(__file__).with_name("bump-own-version.py")
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("bump_own_version", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bov = _load()


class BumpPatch(unittest.TestCase):
    def test_stable_ascii_version_bumps_patch(self):
        self.assertEqual(bov.bump_patch("1.2.3"), "1.2.4")

    def test_prerelease_and_build_versions_are_not_mechanically_bumped(self):
        for version in ("1.2.3-rc.1", "1.2.3+build.1"):
            with self.subTest(version=version):
                with self.assertRaisesRegex(
                    SystemExit, "prerelease/build version"
                ):
                    bov.bump_patch(version)

    def test_non_ascii_numeric_identifiers_are_rejected(self):
        for version in ("1.2٢.3", "1.2.3-1٢"):
            with self.subTest(version=version):
                with self.assertRaisesRegex(SystemExit, "is not semver"):
                    bov.bump_patch(version)


if __name__ == "__main__":
    unittest.main()
