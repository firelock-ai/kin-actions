"""Tests for unattended repository-setting admission."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("check-release-activation.py")
SPEC = importlib.util.spec_from_file_location("check_release_activation", SCRIPT)
assert SPEC and SPEC.loader
activation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(activation)


CONTEXTS = (
    "release / Version bump gate",
    "release / Registry-only build",
    "release / Repo verification",
)


class ReleaseActivationTests(unittest.TestCase):
    def repository(self, **overrides: object) -> dict[str, object]:
        settings: dict[str, object] = {
            "allow_squash_merge": True,
            "allow_merge_commit": False,
            "allow_rebase_merge": False,
            "squash_merge_commit_title": "PR_TITLE",
            "squash_merge_commit_message": "PR_BODY",
        }
        settings.update(overrides)
        return settings

    def protection(self, contexts=CONTEXTS) -> dict[str, object]:
        return {
            "contexts": list(contexts[:1]),
            "checks": [{"context": value} for value in contexts[1:]],
        }

    def test_exact_contract_is_admitted(self) -> None:
        activation.validate(self.repository(), self.protection(), CONTEXTS)

    def test_every_exact_merge_setting_is_required_without_status_contexts(
        self,
    ) -> None:
        invalid = {
            "allow_squash_merge": False,
            "allow_merge_commit": True,
            "allow_rebase_merge": True,
            "squash_merge_commit_title": "COMMIT_OR_PR_TITLE",
            "squash_merge_commit_message": "COMMIT_MESSAGES",
        }
        for setting, value in invalid.items():
            with self.subTest(setting=setting):
                with self.assertRaisesRegex(
                    activation.ActivationError,
                    setting,
                ):
                    activation.validate(
                        self.repository(**{setting: value}),
                        None,
                        (),
                    )

    def test_every_exact_release_context_is_required(self) -> None:
        for missing in CONTEXTS:
            with self.subTest(missing=missing):
                present = tuple(value for value in CONTEXTS if value != missing)
                with self.assertRaisesRegex(
                    activation.ActivationError,
                    missing,
                ):
                    activation.validate(
                        self.repository(),
                        self.protection(present),
                        CONTEXTS,
                    )

    def test_unprotected_main_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            activation.ActivationError,
            "protection JSON is missing",
        ):
            activation.validate(self.repository(), None, CONTEXTS)


if __name__ == "__main__":
    unittest.main()
