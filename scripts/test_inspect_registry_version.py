"""Tests for exact Cargo registry recovery admission."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("inspect-registry-version.py")
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("inspect_registry_version", SCRIPT)
inspect = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = inspect
SPEC.loader.exec_module(inspect)


class RegistryRecoveryAdmissionTests(unittest.TestCase):
    def row(self, version: str, yanked: bool = False):
        return inspect.registry.RegistryVersion(
            name="kin-demo",
            version=version,
            yanked=yanked,
            checksum="a" * 64,
        )

    def test_only_exact_available_row_admits_post_publish_recovery(self) -> None:
        with mock.patch.object(
            inspect.registry,
            "fetch_index",
            return_value=(self.row("0.1.0"), self.row("0.1.1")),
        ):
            result = inspect.inspect(
                "https://kinlab.ai", "kin-demo", "0.1.1"
            )
        self.assertEqual(result["state"], "available")
        self.assertEqual(result["checksum"], "a" * 64)

    def test_missing_index_and_version_are_distinct_waiting_states(self) -> None:
        with mock.patch.object(
            inspect.registry, "fetch_index", return_value=None
        ):
            self.assertEqual(
                inspect.inspect(
                    "https://kinlab.ai", "kin-demo", "0.1.0"
                )["state"],
                "unpublished",
            )
        with mock.patch.object(
            inspect.registry,
            "fetch_index",
            return_value=(self.row("0.1.0"),),
        ):
            self.assertEqual(
                inspect.inspect(
                    "https://kinlab.ai", "kin-demo", "0.1.1"
                )["state"],
                "version-absent",
            )

    def test_yanked_exact_row_fails_release_admission(self) -> None:
        with mock.patch.object(
            inspect.registry,
            "fetch_index",
            return_value=(self.row("0.1.1", yanked=True),),
        ):
            self.assertEqual(
                inspect.inspect(
                    "https://kinlab.ai", "kin-demo", "0.1.1"
                )["state"],
                "yanked",
            )


if __name__ == "__main__":
    unittest.main()
