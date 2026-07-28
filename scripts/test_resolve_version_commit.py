"""Tests for exact version-introducing commit resolution."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("resolve-version-commit.py")
SPEC = importlib.util.spec_from_file_location("resolve_version_commit", SCRIPT)
resolver = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = resolver
SPEC.loader.exec_module(resolver)


class VersionCommitResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "core.hooksPath", "/dev/null")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    def write(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self, message: str) -> str:
        self.git("add", ".")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def test_version_file_returns_oldest_commit_in_current_version_suffix(self) -> None:
        self.write("VERSION", "1.0.0\n")
        self.commit("base")
        self.write("VERSION", "1.0.1\n")
        introduction = self.commit("release")
        self.write("README.md", "later drift\n")
        self.commit("unrelated")

        self.assertEqual(
            resolver.resolve_version_commit(
                root=self.root,
                head_ref="HEAD",
                target_version="1.0.1",
                version_file="VERSION",
                cargo_manifest=None,
            ),
            introduction,
        )

    def test_new_version_starts_a_new_suffix(self) -> None:
        self.write("VERSION", "1.0.0\n")
        self.commit("base")
        self.write("VERSION", "1.0.1\n")
        self.commit("patch")
        self.write("VERSION", "1.1.0\n")
        introduction = self.commit("minor")
        self.write("README.md", "post release work\n")
        self.commit("later")

        self.assertEqual(
            resolver.resolve_version_commit(
                root=self.root,
                head_ref="HEAD",
                target_version="1.1.0",
                version_file="VERSION",
                cargo_manifest=None,
            ),
            introduction,
        )

    def test_virtual_workspace_root_version_is_supported(self) -> None:
        self.write(
            "Cargo.toml",
            '[workspace]\nmembers = ["crates/x"]\n'
            '[workspace.package]\nversion = "0.7.0"\n',
        )
        self.write(
            "crates/x/Cargo.toml",
            '[package]\nname = "x"\nversion.workspace = true\n',
        )
        self.commit("base")
        self.write(
            "Cargo.toml",
            '[workspace]\nmembers = ["crates/x"]\n'
            '[workspace.package]\nversion = "0.7.1"\n',
        )
        introduction = self.commit("release")
        self.write("README.md", "later\n")
        self.commit("later")

        self.assertEqual(
            resolver.resolve_version_commit(
                root=self.root,
                head_ref="HEAD",
                target_version="0.7.1",
                version_file=None,
                cargo_manifest="Cargo.toml",
            ),
            introduction,
        )

    def test_head_version_mismatch_fails_closed(self) -> None:
        self.write("VERSION", "1.0.0\n")
        self.commit("base")
        with self.assertRaisesRegex(
            resolver.VersionCommitError, "HEAD carries version"
        ):
            resolver.resolve_version_commit(
                root=self.root,
                head_ref="HEAD",
                target_version="1.0.1",
                version_file="VERSION",
                cargo_manifest=None,
            )


if __name__ == "__main__":
    unittest.main()
