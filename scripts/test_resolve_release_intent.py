"""Adversarial tests for immutable automatic release intent."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("resolve-release-intent.py")
SPEC = importlib.util.spec_from_file_location("resolve_release_intent", SCRIPT)
resolver = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = resolver
SPEC.loader.exec_module(resolver)


class ImmutableIntentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "core.hooksPath", "/dev/null")
        (self.root / "source").write_text("base\n", encoding="utf-8")
        self.base = self.commit("base")

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

    def commit(self, message: str) -> str:
        self.git("add", ".")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def change(self, value: str, message: str) -> str:
        (self.root / "source").write_text(value + "\n", encoding="utf-8")
        return self.commit(message)

    def resolve(self):
        return resolver.resolve_release_intent(
            root=self.root,
            base_ref=self.base,
        )

    def test_source_drift_defaults_to_patch(self) -> None:
        self.change("one", "ordinary source")
        self.assertEqual(self.resolve()["intent"], "patch")

    def test_highest_landed_first_parent_trailer_wins(self) -> None:
        minor = self.change(
            "one",
            "feature\n\nKin-Release-Intent: minor",
        )
        major = self.change(
            "two",
            "breaking\n\nKin-Release-Intent: major",
        )
        result = self.resolve()
        self.assertEqual(result["intent"], "major")
        self.assertEqual(
            result["evidence"],
            [
                {"commit": minor, "intent": "minor"},
                {"commit": major, "intent": "major"},
            ],
        )

    def test_duplicate_invalid_and_non_footer_evidence_fail_closed(self) -> None:
        messages = (
            "bad\n\nKin-Release-Intent: minor\nKin-Release-Intent: major",
            "bad\n\nKin-Release-Intent: banana",
            "Kin-Release-Intent: minor\n\nbody after trailer",
        )
        for index, message in enumerate(messages):
            with self.subTest(message=message):
                self.git("reset", "--hard", self.base)
                self.change(str(index), message)
                with self.assertRaises(resolver.ReleaseIntentError):
                    self.resolve()


if __name__ == "__main__":
    unittest.main()
