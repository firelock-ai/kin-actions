"""Black-box tests for bounded idempotent Cargo release recovery."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("recover-cargo-release.sh")
HEAD = "a" * 40
MARKER = "<!-- kin-cargo-release:downstreams-dispatched -->"


class CargoReleaseRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.helper = self.root / "helper"
        self.bin.mkdir()
        (self.helper / "scripts").mkdir(parents=True)
        self.output = self.root / "output"
        self.summary = self.root / "summary"
        self.log = self.root / "mutations"
        self.release_views = self.root / "release-views"

        self.executable(
            self.bin / "python3",
            f"""#!/usr/bin/env bash
case "$1" in
  *prepare-cargo-release.py)
    printf '%s\\n' '{{"current_version":"1.2.3"}}'
    ;;
  *resolve-version-commit.py)
    printf '%s\\n' '{HEAD}'
    ;;
  *inspect-registry-version.py)
    printf '{{"state":"%s"}}\\n' "${{FAKE_REGISTRY_STATE}}"
    ;;
  *)
    exec {shlex.quote(sys.executable)} "$@"
    ;;
esac
""",
        )
        self.executable(
            self.bin / "git",
            f"""#!/usr/bin/env bash
if [[ "$1" == "rev-parse" && "$2" == "HEAD" ]]; then
  printf '%s\\n' '{HEAD}'
  exit 0
fi
if [[ "$1" == "ls-remote" ]]; then
  if [[ "$FAKE_TAG_PRESENT" == "true" ]]; then
    printf '%s\\t%s\\n' '{HEAD}' 'refs/tags/v1.2.3'
  fi
  exit 0
fi
echo "unexpected git: $*" >&2
exit 9
""",
        )
        self.executable(
            self.bin / "gh",
            f"""#!/usr/bin/env bash
case "$1 $2" in
  "api repos/"*)
    printf '%s\\n' '{HEAD}'
    ;;
  "run list")
    printf '%s\\n' success
    ;;
  "release view")
    count=0
    if [[ -f "$FAKE_RELEASE_VIEWS" ]]; then
      count="$(cat "$FAKE_RELEASE_VIEWS")"
    fi
    count=$((count + 1))
    printf '%s' "$count" > "$FAKE_RELEASE_VIEWS"
    if [[ "$FAKE_RELEASE_INITIAL_ABSENT" == "true" && "$count" == "1" ]]; then
      exit 0
    fi
    printf '%s\\n' "$FAKE_RELEASE_JSON"
    ;;
  "release edit")
    printf '%s\\n' release-edit >> "$FAKE_MUTATION_LOG"
    ;;
  *)
    echo "unexpected gh: $*" >&2
    exit 8
    ;;
esac
""",
        )
        self.executable(
            self.bin / "timeout",
            """#!/usr/bin/env bash
shift
exec "$@"
""",
        )
        for name in (
            "consumer-smoke.sh",
            "dispatch-downstreams.sh",
            "mint-release-tag.sh",
            "wait-tag-release-run.sh",
        ):
            failure = (
                'if [[ "${FAKE_MINT_FAIL:-}" == "true" ]]; then exit 7; fi\n'
                if name == "mint-release-tag.sh"
                else (
                    'if [[ "${FAKE_RELEASE_RUN_FAIL:-}" == "true" ]]; then exit 6; fi\n'
                    if name == "wait-tag-release-run.sh"
                    else ""
                )
            )
            output = (
                'printf \'%s\\n\' \'{"run_id":123,"attempt":1,"url":"https://example.test/run"}\'\n'
                if name == "wait-tag-release-run.sh"
                else f'printf "%s\\n" "{name}" >> "$FAKE_MUTATION_LOG"\n'
            )
            self.executable(
                self.helper / "scripts" / name,
                "#!/usr/bin/env bash\n"
                + failure
                + output,
            )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def executable(path: Path, body: str) -> None:
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def run_recovery(
        self,
        *,
        registry_state: str,
        body: str = MARKER,
        mint_fails: bool = False,
        tag_present: bool = True,
        release_run_fails: bool = False,
        initial_release_absent: bool = False,
    ) -> subprocess.CompletedProcess:
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{self.bin}{os.pathsep}{environment['PATH']}",
                "PACKAGE": "kin-demo",
                "MANIFEST": "Cargo.toml",
                "KIN_ACTIONS_HELPER": str(self.helper),
                "KIN_READ_TOKEN": "read",
                "KIN_ACTIONS_TOKEN": "actions",
                "KIN_RELEASE_TAG_TOKEN": "release",
                "KIN_DOWNSTREAM_DISPATCH_TOKEN": "dispatch",
                "KIN_RECOVERY_SUMMARY": str(self.summary),
                "GITHUB_OUTPUT": str(self.output),
                "GITHUB_REPOSITORY": "firelock-ai/kin-demo",
                "GITHUB_EVENT_REPOSITORY_DEFAULT_BRANCH": "main",
                "FAKE_REGISTRY_STATE": registry_state,
                "FAKE_RELEASE_JSON": json.dumps(
                    {
                        "isDraft": False,
                        "isPrerelease": False,
                        "body": body,
                    }
                ),
                "FAKE_MUTATION_LOG": str(self.log),
                "FAKE_RELEASE_VIEWS": str(self.release_views),
                "FAKE_MINT_FAIL": "true" if mint_fails else "false",
                "FAKE_TAG_PRESENT": "true" if tag_present else "false",
                "FAKE_RELEASE_RUN_FAIL": (
                    "true" if release_run_fails else "false"
                ),
                "FAKE_RELEASE_INITIAL_ABSENT": (
                    "true" if initial_release_absent else "false"
                ),
            }
        )
        return subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=self.root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def outputs(self) -> dict[str, str]:
        return dict(
            line.split("=", 1)
            for line in self.output.read_text(encoding="utf-8").splitlines()
        )

    def mutations(self) -> list[str]:
        if not self.log.exists():
            return []
        return self.log.read_text(encoding="utf-8").splitlines()

    def test_absent_registry_row_authorizes_no_post_publish_action(self) -> None:
        result = self.run_recovery(registry_state="version-absent")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.outputs()["recovery_state"], "awaiting-publication"
        )
        self.assertEqual(self.mutations(), [])

    def test_existing_delivery_marker_makes_recovery_idempotent(self) -> None:
        result = self.run_recovery(registry_state="available")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs()["recovery_state"], "complete")
        self.assertEqual(
            self.mutations(),
            [],
        )

    def test_missing_marker_dispatches_then_persists_marker(self) -> None:
        result = self.run_recovery(registry_state="available", body="notes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.mutations(),
            [
                "consumer-smoke.sh",
                "release-edit",
                "dispatch-downstreams.sh",
                "release-edit",
            ],
        )
        self.assertEqual(self.outputs()["recovery_state"], "complete")

    def test_tag_failure_stops_before_release_or_downstreams(self) -> None:
        result = self.run_recovery(
            registry_state="available",
            body="notes",
            mint_fails=True,
            tag_present=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            self.mutations(),
            ["consumer-smoke.sh"],
        )

    def test_failed_tag_release_gate_cannot_finalize_or_dispatch(self) -> None:
        result = self.run_recovery(
            registry_state="available",
            body="notes",
            release_run_fails=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.mutations(), ["consumer-smoke.sh"])
        self.assertNotIn("release create", result.stdout + result.stderr)

    def test_release_view_race_is_refreshed_only_after_gate_success(self) -> None:
        result = self.run_recovery(
            registry_state="available",
            body="notes",
            initial_release_absent=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(
            int(self.release_views.read_text(encoding="utf-8")),
            2,
        )
        self.assertNotIn("release create", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
