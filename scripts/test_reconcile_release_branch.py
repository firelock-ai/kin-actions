#!/usr/bin/env python3
"""Adversarial tests for release-train branch reconciliation."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def _load_module():
    path = Path(__file__).with_name("reconcile-release-branch.py")
    spec = importlib.util.spec_from_file_location("reconcile_release_branch", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rrb = _load_module()


class GitFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.git("init", "-b", "main")
        (self.root / ".git" / "no-hooks").mkdir()
        self.git("config", "core.hooksPath", ".git/no-hooks")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "user.name", "Release Test")
        self.git("config", "user.email", "release-test@example.invalid")

    def close(self) -> None:
        self.temporary.cleanup()

    def git(
        self,
        *args: str,
        check: bool = True,
        input_text: str | None = None,
    ) -> str:
        result = subprocess.run(
            ["git", "-C", os.fspath(self.root), *args],
            check=False,
            capture_output=True,
            text=True,
            input=input_text,
        )
        if check and result.returncode != 0:
            raise AssertionError(
                f"git {' '.join(args)} failed\nstdout={result.stdout}\nstderr={result.stderr}"
            )
        return result.stdout.strip()

    def write(self, path: str, contents: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")

    def remove(self, path: str) -> None:
        (self.root / path).unlink()

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD")

    def tree(self, treeish: str) -> str:
        return self.git("rev-parse", f"{treeish}^{{tree}}")

    def raw_commit(self, treeish: str, parents: list[str]) -> str:
        args = ["commit-tree", self.tree(treeish)]
        for parent in parents:
            args.extend(["-p", parent])
        return self.git(*args, input_text="synthetic API merge\n")


class ReleaseTrainCase(unittest.TestCase):
    generated = ["Cargo.lock", "Cargo.toml"]

    def setUp(self) -> None:
        self.repo = GitFixture()
        self.repo.write("Cargo.toml", '[package]\nname = "demo"\nversion = "0.1.0"\n')
        self.repo.write("Cargo.lock", "version = 3\npackage = 0.1.0\n")
        self.repo.write("README.md", "stable\n")
        self.repo.write(".github/workflows/ci.yml", "name: CI\n# v1\n")
        self.base = self.repo.commit("base")

        self.repo.git("checkout", "-b", "automation/release-next")
        self.repo.write(
            "Cargo.toml", '[package]\nname = "demo"\nversion = "0.1.1"\n'
        )
        self.repo.write("Cargo.lock", "version = 3\npackage = 0.1.1\n")
        self.train = self.repo.commit("prepare release")

        self.repo.git("checkout", "main")
        self.repo.write(".github/workflows/ci.yml", "name: CI\n# trusted v2\n")
        self.main = self.repo.commit("harden workflow")
        self.repo.git("checkout", "automation/release-next")

    def tearDown(self) -> None:
        self.repo.close()

    def neutralize(self) -> dict[str, object]:
        return rrb.neutralize(
            self.repo.root,
            trusted_main=self.main,
            old_train_head=self.repo.git("rev-parse", "HEAD"),
            generated_paths=self.generated,
        )

    def commit_neutralization(self) -> str:
        result = self.neutralize()
        self.assertTrue(result["changed"])
        return self.repo.commit("neutralize generated files")

    def create_valid_merge(self) -> tuple[str, str]:
        neutralized = self.commit_neutralization()
        self.repo.git("merge", "--no-ff", self.main, "-m", "merge trusted main")
        return neutralized, self.repo.git("rev-parse", "HEAD")

    def scratch_tree(
        self,
        *,
        write: tuple[str, str] | None = None,
        executable: str | None = None,
        symlink: tuple[str, str] | None = None,
    ) -> str:
        self.repo.git("checkout", "-b", "scratch", self.main)
        if write:
            self.repo.write(*write)
        if executable:
            (self.repo.root / executable).chmod(0o755)
        if symlink:
            path, target = symlink
            candidate = self.repo.root / path
            if candidate.exists() or candidate.is_symlink():
                candidate.unlink()
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.symlink_to(target)
        commit = self.repo.commit("scratch tree")
        return commit

    def checkout_raw_merge(self, treeish: str, parents: list[str]) -> str:
        merge = self.repo.raw_commit(treeish, parents)
        self.repo.git("checkout", "--detach", merge)
        return merge


class PathValidation(unittest.TestCase):
    def test_accepts_exact_normalized_files(self) -> None:
        self.assertEqual(
            rrb.validate_generated_paths(["Cargo.toml", "crates/a/Cargo.lock"]),
            ("Cargo.toml", "crates/a/Cargo.lock"),
        )

    def test_rejects_empty_absolute_traversal_and_non_normal_paths(self) -> None:
        for path in ("", "/Cargo.lock", "../Cargo.lock", "a/../Cargo.lock", "a//b"):
            with self.subTest(path=path), self.assertRaises(rrb.InvariantError):
                rrb.validate_generated_paths([path])

    def test_rejects_windows_and_control_char_paths(self) -> None:
        for path in ("a\\b", "Cargo.lock\nignored"):
            with self.subTest(path=path), self.assertRaises(rrb.InvariantError):
                rrb.validate_generated_paths([path])

    def test_rejects_git_metadata_workflows_and_duplicates(self) -> None:
        for paths in (
            [".git/config"],
            [".github/workflows/release.yml"],
            ["Cargo.lock", "Cargo.lock"],
        ):
            with self.subTest(paths=paths), self.assertRaises(rrb.InvariantError):
                rrb.validate_generated_paths(paths)


class NeutralizeReleaseTrain(ReleaseTrainCase):
    def test_stages_only_generated_paths_at_exact_main_tree_entries(self) -> None:
        result = self.neutralize()
        self.assertEqual(result["changed_paths"], ["Cargo.lock", "Cargo.toml"])
        staged = self.repo.git("diff", "--cached", "--name-only").splitlines()
        self.assertEqual(staged, ["Cargo.lock", "Cargo.toml"])
        for path in self.generated:
            self.assertEqual(
                self.repo.git("show", f":{path}"),
                self.repo.git("show", f"{self.main}:{path}"),
            )
            index_fields = self.repo.git(
                "ls-files", "--stage", "--", path
            ).split()
            tree_fields = self.repo.git("ls-tree", self.main, "--", path).split()
            self.assertEqual(
                [index_fields[0], index_fields[1]],
                [tree_fields[0], tree_fields[2]],
            )

    def test_trusted_main_generated_update_wins_exactly(self) -> None:
        self.repo.git("checkout", "main")
        self.repo.write(
            "Cargo.toml", '[package]\nname = "demo"\nversion = "0.1.7"\n'
        )
        self.main = self.repo.commit("main owns newer generated version")
        self.repo.git("checkout", "automation/release-next")
        self.neutralize()
        self.assertIn(
            'version = "0.1.7"', (self.repo.root / "Cargo.toml").read_text()
        )

    def test_trusted_main_deletion_removes_generated_file(self) -> None:
        self.repo.git("checkout", "main")
        self.repo.remove("Cargo.lock")
        self.main = self.repo.commit("remove generated lock")
        self.repo.git("checkout", "automation/release-next")
        self.neutralize()
        self.assertFalse((self.repo.root / "Cargo.lock").exists())
        self.assertIn("Cargo.lock", self.repo.git("diff", "--cached", "--name-only"))

    def test_trusted_main_mode_wins_exactly(self) -> None:
        self.repo.git("checkout", "main")
        (self.repo.root / "Cargo.lock").chmod(0o755)
        self.main = self.repo.commit("make generated file executable")
        self.repo.git("checkout", "automation/release-next")
        self.neutralize()
        self.assertTrue((self.repo.root / "Cargo.lock").stat().st_mode & 0o111)
        self.assertTrue(
            self.repo.git("ls-files", "--stage", "Cargo.lock").startswith("100755 ")
        )

    def test_rejects_non_generated_train_delta(self) -> None:
        self.repo.write("src/lib.rs", "malicious\n")
        self.train = self.repo.commit("smuggle source")
        with self.assertRaisesRegex(rrb.InvariantError, "non-generated paths"):
            self.neutralize()

    def test_rejects_untracked_worktree_content(self) -> None:
        self.repo.write("untracked.txt", "surprise\n")
        with self.assertRaisesRegex(rrb.InvariantError, "including untracked"):
            self.neutralize()

    def test_rejects_generated_symlink(self) -> None:
        self.repo.remove("Cargo.lock")
        (self.repo.root / "Cargo.lock").symlink_to("README.md")
        self.train = self.repo.commit("replace generated file with symlink")
        with self.assertRaisesRegex(rrb.InvariantError, "symlink"):
            self.neutralize()

    def test_rejects_symlink_in_trusted_main_tree(self) -> None:
        self.repo.git("checkout", "main")
        (self.repo.root / "trusted-link").symlink_to("README.md")
        self.main = self.repo.commit("add trusted symlink")
        self.repo.git("checkout", "automation/release-next")
        with self.assertRaisesRegex(rrb.InvariantError, "symlink"):
            self.neutralize()

    def test_rejects_allowlist_typo_absent_from_all_trees(self) -> None:
        with self.assertRaisesRegex(rrb.InvariantError, "absent from all"):
            rrb.neutralize(
                self.repo.root,
                trusted_main=self.main,
                old_train_head=self.train,
                generated_paths=["Cargo.toml", "does-not-exist.lock"],
            )

    def test_rejects_wrong_checked_out_train_head(self) -> None:
        with self.assertRaisesRegex(rrb.InvariantError, "HEAD must equal"):
            rrb.neutralize(
                self.repo.root,
                trusted_main=self.main,
                old_train_head=self.base,
                generated_paths=self.generated,
            )

    def test_rejects_malformed_and_missing_commit_ids(self) -> None:
        for value in ("HEAD", "a" * 39, "A" * 40, "f" * 40):
            with self.subTest(value=value), self.assertRaises(rrb.InvariantError):
                rrb.neutralize(
                    self.repo.root,
                    trusted_main=value,
                    old_train_head=self.train,
                    generated_paths=self.generated,
                )


class ValidateServerMerge(ReleaseTrainCase):
    def validate(self, merge: str, train: str) -> dict[str, object]:
        return rrb.validate_merge(
            self.repo.root,
            merge_commit=merge,
            trusted_main=self.main,
            old_train_head=train,
            generated_paths=self.generated,
        )

    def test_accepts_exact_ordered_two_parent_merge(self) -> None:
        train, merge = self.create_valid_merge()
        result = self.validate(merge, train)
        self.assertTrue(result["validated"])
        self.assertEqual(result["parents"], [train, self.main])

    def test_rejects_reversed_parent_order(self) -> None:
        train = self.commit_neutralization()
        merge = self.checkout_raw_merge(self.main, [self.main, train])
        with self.assertRaisesRegex(rrb.InvariantError, "ordered parents"):
            self.validate(merge, train)

    def test_rejects_octopus_merge(self) -> None:
        train = self.commit_neutralization()
        merge = self.checkout_raw_merge(
            self.main, [train, self.main, self.base]
        )
        with self.assertRaisesRegex(rrb.InvariantError, "ordered parents"):
            self.validate(merge, train)

    def test_rejects_single_parent_commit(self) -> None:
        train = self.commit_neutralization()
        merge = self.checkout_raw_merge(self.main, [train])
        with self.assertRaisesRegex(rrb.InvariantError, "ordered parents"):
            self.validate(merge, train)

    def test_rejects_generated_bytes_before_regeneration_gate(self) -> None:
        train = self.commit_neutralization()
        scratch = self.scratch_tree(
            write=(
                "Cargo.toml",
                '[package]\nname = "demo"\nversion = "9.9.9"\n',
            )
        )
        merge = self.checkout_raw_merge(scratch, [train, self.main])
        with self.assertRaisesRegex(rrb.InvariantError, "generated paths"):
            self.validate(merge, train)

    def test_rejects_non_generated_byte_drift(self) -> None:
        train = self.commit_neutralization()
        scratch = self.scratch_tree(write=("README.md", "tampered\n"))
        merge = self.checkout_raw_merge(scratch, [train, self.main])
        with self.assertRaisesRegex(rrb.InvariantError, "outside generated"):
            self.validate(merge, train)

    def test_rejects_non_generated_mode_drift(self) -> None:
        train = self.commit_neutralization()
        scratch = self.scratch_tree(executable="README.md")
        merge = self.checkout_raw_merge(scratch, [train, self.main])
        with self.assertRaisesRegex(rrb.InvariantError, "outside generated"):
            self.validate(merge, train)

    def test_rejects_extra_tree_path(self) -> None:
        train = self.commit_neutralization()
        scratch = self.scratch_tree(write=("extra.txt", "unexpected\n"))
        merge = self.checkout_raw_merge(scratch, [train, self.main])
        with self.assertRaisesRegex(rrb.InvariantError, "extra.txt"):
            self.validate(merge, train)

    def test_rejects_unneutralized_train_parent(self) -> None:
        merge = self.checkout_raw_merge(self.main, [self.train, self.main])
        with self.assertRaisesRegex(rrb.InvariantError, "not neutralized"):
            self.validate(merge, self.train)

    def test_rejects_merge_not_checked_out(self) -> None:
        train = self.commit_neutralization()
        merge = self.repo.raw_commit(self.main, [train, self.main])
        with self.assertRaisesRegex(rrb.InvariantError, "HEAD must equal"):
            self.validate(merge, train)

    def test_rejects_untracked_content_during_validation(self) -> None:
        train, merge = self.create_valid_merge()
        self.repo.write("untracked.txt", "surprise\n")
        with self.assertRaisesRegex(rrb.InvariantError, "including untracked"):
            self.validate(merge, train)

    def test_rejects_malformed_missing_and_non_commit_merge_ids(self) -> None:
        train = self.commit_neutralization()
        tree_oid = self.repo.tree(self.main)
        for value in ("HEAD", "a" * 39, "f" * 40, tree_oid):
            with self.subTest(value=value), self.assertRaises(rrb.InvariantError):
                rrb.validate_merge(
                    self.repo.root,
                    merge_commit=value,
                    trusted_main=self.main,
                    old_train_head=train,
                    generated_paths=self.generated,
                )


class CliContract(ReleaseTrainCase):
    def test_neutralize_prints_machine_readable_json(self) -> None:
        result = subprocess.run(
            [
                "python3",
                os.fspath(Path(rrb.__file__)),
                "neutralize",
                "--repo",
                os.fspath(self.repo.root),
                "--trusted-main",
                self.main,
                "--old-train-head",
                self.train,
                "--generated-path",
                "Cargo.toml",
                "--generated-path",
                "Cargo.lock",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"changed": true', result.stdout)
        self.assertEqual(result.stderr, "")

    def test_cli_failure_is_nonzero_and_concise(self) -> None:
        result = subprocess.run(
            [
                "python3",
                os.fspath(Path(rrb.__file__)),
                "neutralize",
                "--repo",
                os.fspath(self.repo.root),
                "--trusted-main",
                "not-a-sha",
                "--old-train-head",
                self.train,
                "--generated-path",
                "Cargo.toml",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("release-branch invariant failed:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
