"""Regression tests for exact remote release-tag admission."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("mint-release-tag.sh")


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True
    ).strip()


class ExistingRemoteTag(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repo = root / "source"
        self.remote = root / "remote.git"
        self.output = root / "output"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        _git(self.repo, "config", "user.name", "Release Test")
        _git(self.repo, "config", "user.email", "release-test@example.com")
        (self.repo / "payload").write_text("one\n")
        _git(self.repo, "add", "payload")
        _git(self.repo, "commit", "-q", "-s", "-m", "first")
        self.first = _git(self.repo, "rev-parse", "HEAD")
        (self.repo / "payload").write_text("two\n")
        _git(self.repo, "commit", "-q", "-s", "-am", "second")
        self.second = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "tag", "-a", "v1.2.3", "-m", "Release 1.2.3", self.first)
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.repo), str(self.remote)],
            check=True,
        )
        _git(self.repo, "remote", "add", "origin", str(self.remote))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_mint(self, sha: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=self.repo,
            env={
                **os.environ,
                "VERSION": "1.2.3",
                "GITHUB_SHA": sha,
                "GITHUB_REPOSITORY": "firelock-ai/example",
                "GITHUB_OUTPUT": str(self.output),
            },
            text=True,
            capture_output=True,
            check=False,
        )

    def test_exact_remote_tag_is_reported_complete(self) -> None:
        result = self.run_mint(self.first)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("release tag is complete", result.stdout)
        self.assertEqual(self.output.read_text(), "release_tag_status=already-present\n")

    def test_remote_tag_at_other_commit_fails_closed(self) -> None:
        result = self.run_mint(self.second)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not the published commit", result.stderr)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
