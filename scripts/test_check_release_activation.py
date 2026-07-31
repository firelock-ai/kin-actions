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
GITHUB_ACTIONS_APP_ID = 15368


class ReleaseActivationTests(unittest.TestCase):
    def repository(self, **overrides: object) -> dict[str, object]:
        settings: dict[str, object] = {
            "allow_auto_merge": True,
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
            "strict": True,
            "contexts": list(contexts),
            "checks": [
                {"context": value, "app_id": GITHUB_ACTIONS_APP_ID}
                for value in contexts
            ],
        }

    def test_exact_contract_is_admitted(self) -> None:
        activation.validate(
            self.repository(),
            self.protection(),
            CONTEXTS,
            GITHUB_ACTIONS_APP_ID,
        )

    def test_every_exact_merge_setting_is_required_without_status_contexts(
        self,
    ) -> None:
        invalid = {
            "allow_auto_merge": False,
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
                        GITHUB_ACTIONS_APP_ID,
                    )

    def test_unprotected_main_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            activation.ActivationError,
            "protection JSON is missing",
        ):
            activation.validate(
                self.repository(),
                None,
                CONTEXTS,
                GITHUB_ACTIONS_APP_ID,
            )

    def test_non_strict_or_unreported_up_to_date_policy_fails_closed(
        self,
    ) -> None:
        for strict in (False, None):
            with self.subTest(strict=strict):
                protection = self.protection()
                if strict is None:
                    del protection["strict"]
                else:
                    protection["strict"] = strict
                with self.assertRaisesRegex(
                    activation.ActivationError,
                    "strict/up-to-date",
                ):
                    activation.validate(
                        self.repository(),
                        protection,
                        CONTEXTS,
                        GITHUB_ACTIONS_APP_ID,
                    )

    def test_unbound_legacy_context_does_not_satisfy_release_check(self) -> None:
        protection = {
            "strict": True,
            "contexts": list(CONTEXTS),
            "checks": [],
        }
        with self.assertRaisesRegex(
            activation.ActivationError,
            "App-bound",
        ):
            activation.validate(
                self.repository(),
                protection,
                CONTEXTS,
                GITHUB_ACTIONS_APP_ID,
            )

    def test_null_or_wrong_app_binding_fails_closed(self) -> None:
        for app_id in (None, 1):
            with self.subTest(app_id=app_id):
                protection = self.protection()
                checks = protection["checks"]
                assert isinstance(checks, list)
                checks[0] = {"context": CONTEXTS[0], "app_id": app_id}
                with self.assertRaisesRegex(
                    activation.ActivationError,
                    "App-bound",
                ):
                    activation.validate(
                        self.repository(),
                        protection,
                        CONTEXTS,
                        GITHUB_ACTIONS_APP_ID,
                    )

    def test_duplicate_wrong_writer_for_release_check_fails_closed(self) -> None:
        protection = self.protection()
        checks = protection["checks"]
        assert isinstance(checks, list)
        checks.append({"context": CONTEXTS[0], "app_id": None})
        with self.assertRaisesRegex(
            activation.ActivationError,
            "unbound or wrong App writer",
        ):
            activation.validate(
                self.repository(),
                protection,
                CONTEXTS,
                GITHUB_ACTIONS_APP_ID,
            )

    def test_duplicate_identical_writer_fails_closed(self) -> None:
        protection = self.protection()
        checks = protection["checks"]
        assert isinstance(checks, list)
        checks.append(
            {
                "context": CONTEXTS[0],
                "app_id": GITHUB_ACTIONS_APP_ID,
            }
        )
        with self.assertRaisesRegex(
            activation.ActivationError,
            "unbound or wrong App writer",
        ):
            activation.validate(
                self.repository(),
                protection,
                CONTEXTS,
                GITHUB_ACTIONS_APP_ID,
            )

    def test_required_check_app_id_is_mandatory(self) -> None:
        with self.assertRaisesRegex(
            activation.ActivationError,
            "positive required-check App ID",
        ):
            activation.validate(
                self.repository(),
                self.protection(),
                CONTEXTS,
            )


if __name__ == "__main__":
    unittest.main()
