"""Adversarial tests for fail-closed reusable registry builds."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("run-registry-build.py")
SPEC = importlib.util.spec_from_file_location("run_registry_build", SCRIPT)
build = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = build
SPEC.loader.exec_module(build)


class RegistryBuildGrammarTests(unittest.TestCase):
    def test_empty_command_uses_exact_package_build(self) -> None:
        self.assertEqual(
            build.parse_commands("", "kin-blobs"),
            [["cargo", "build", "-p", "kin-blobs", "--manifest-path", "Cargo.toml"]],
        )

    def test_current_multi_build_callers_remain_supported(self) -> None:
        self.assertEqual(
            build.parse_commands(
                "cargo build && cargo build --no-default-features --features vector",
                "kin-db",
            ),
            [
                ["cargo", "build", "--manifest-path", "Cargo.toml"],
                [
                    "cargo",
                    "build",
                    "--no-default-features",
                    "--features",
                    "vector",
                    "--manifest-path",
                    "Cargo.toml",
                ],
            ],
        )

    def test_shell_masking_and_non_build_commands_fail_closed(self) -> None:
        invalid = (
            "cargo build || true",
            "cargo build; true",
            "cargo build | tee build.log",
            "cargo build\ntrue",
            "if ! cargo build; then true; fi",
            "set +e && cargo build",
            "cargo test",
        )
        for command in invalid:
            with self.subTest(command=command), self.assertRaises(
                build.BuildCommandError
            ):
                build.parse_commands(command, "kin-db")

    def test_target_and_config_authority_cannot_be_overridden(self) -> None:
        invalid = (
            "cargo build --target-dir /home/runner/.cargo/git/target",
            "cargo build --target-dir=/tmp/other",
            "cargo build --config build.target-dir=/tmp/other",
            "cargo build --config=build.target-dir=/tmp/other",
        )
        for command in invalid:
            with self.subTest(command=command), self.assertRaisesRegex(
                build.BuildCommandError, "owned by the workflow"
            ):
                build.parse_commands(command, "kin-db")

    def test_build_manifest_must_equal_the_prefetched_manifest(self) -> None:
        self.assertEqual(
            build.parse_commands(
                "cargo build --manifest-path crates/one/Cargo.toml",
                "kin-db",
                "crates/one/Cargo.toml",
            ),
            [["cargo", "build", "--manifest-path", "crates/one/Cargo.toml"]],
        )
        for command in (
            "cargo build --manifest-path crates/two/Cargo.toml",
            "cargo build --manifest-path=crates/two/Cargo.toml",
        ):
            with self.subTest(command=command), self.assertRaisesRegex(
                build.BuildCommandError, "prefetched manifest"
            ):
                build.parse_commands(command, "kin-db", "crates/one/Cargo.toml")

    def test_first_failed_build_stops_the_sequence(self) -> None:
        commands = build.parse_commands(
            "cargo build && cargo build --features vector", "kin-db"
        )
        with mock.patch.object(
            build.subprocess,
            "run",
            side_effect=subprocess.CalledProcessError(1, commands[0]),
        ) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                build.run_commands(commands)
        run.assert_called_once_with(commands[0], check=True)


if __name__ == "__main__":
    unittest.main()
