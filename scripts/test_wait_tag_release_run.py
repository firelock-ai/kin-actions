"""Black-box tests for bounded exact tag Release workflow admission."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("wait-tag-release-run.sh")
HEAD = "a" * 40


class WaitTagReleaseRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.sequence = self.root / "sequence"
        self.reruns = self.root / "reruns"
        self._executable(
            self.bin / "gh",
            f"""#!/usr/bin/env bash
if [[ "$1 $2" == "run list" ]]; then
  first="$(head -n 1 "$FAKE_SEQUENCE")"
  if [[ -s "$FAKE_SEQUENCE" ]]; then
    tail -n +2 "$FAKE_SEQUENCE" > "$FAKE_SEQUENCE.next"
    mv "$FAKE_SEQUENCE.next" "$FAKE_SEQUENCE"
  fi
  printf '%s\\n' "$first"
  exit 0
fi
if [[ "$1 $2" == "run rerun" ]]; then
  printf '%s\\n' "$*" >> "$FAKE_RERUNS"
  exit 0
fi
echo "unexpected gh: $*" >&2
exit 8
""",
        )
        self._executable(
            self.bin / "sleep",
            "#!/usr/bin/env bash\nexit 0\n",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _executable(path: Path, body: str) -> None:
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    @staticmethod
    def run_json(
        *,
        status: str,
        conclusion: str = "",
        attempt: int = 1,
        branch: str = "v1.2.3",
        sha: str = HEAD,
    ) -> str:
        return json.dumps(
            [
                {
                    "attempt": attempt,
                    "conclusion": conclusion,
                    "createdAt": "2026-07-28T00:00:00Z",
                    "databaseId": 123,
                    "event": "push",
                    "headBranch": branch,
                    "headSha": sha,
                    "status": status,
                    "url": "https://github.com/firelock-ai/demo/actions/runs/123",
                    "workflowName": "Release",
                }
            ]
        )

    def run_helper(
        self,
        sequence: list[str],
        *,
        timeout: int = 1,
        attempts: int = 3,
    ):
        self.sequence.write_text("\n".join(sequence) + "\n", encoding="utf-8")
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{self.bin}{os.pathsep}{environment['PATH']}",
                "GITHUB_REPOSITORY": "firelock-ai/demo",
                "TAG": "v1.2.3",
                "VERSION_COMMIT": HEAD,
                "RELEASE_WORKFLOW": "Release",
                "KIN_ACTIONS_TOKEN": "actions",
                "KIN_RELEASE_RUN_TIMEOUT_SECONDS": str(timeout),
                "KIN_RELEASE_RUN_POLL_SECONDS": "0",
                "KIN_RELEASE_RUN_MAX_ATTEMPTS": str(attempts),
                "FAKE_SEQUENCE": str(self.sequence),
                "FAKE_RERUNS": str(self.reruns),
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

    def test_running_exact_run_is_waited_to_success(self) -> None:
        result = self.run_helper(
            [
                self.run_json(status="in_progress"),
                self.run_json(status="completed", conclusion="success"),
            ]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["run_id"], 123)
        self.assertFalse(self.reruns.exists())

    def test_failed_run_is_automatically_rerun_within_bound(self) -> None:
        result = self.run_helper(
            [
                self.run_json(status="completed", conclusion="failure"),
                self.run_json(status="in_progress", attempt=2),
                self.run_json(
                    status="completed",
                    conclusion="success",
                    attempt=2,
                ),
            ]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("run rerun 123", self.reruns.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(result.stdout)["attempt"], 2)

    def test_failed_terminal_attempt_fails_closed(self) -> None:
        result = self.run_helper(
            [self.run_json(status="completed", conclusion="failure", attempt=3)]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bounded attempt 3", result.stderr)
        self.assertFalse(self.reruns.exists())

    def test_absent_or_wrong_tag_run_times_out(self) -> None:
        wrong = self.run_json(
            status="completed",
            conclusion="success",
            branch="main",
        )
        result = self.run_helper([wrong] * 100, timeout=0)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not succeed", result.stderr)


if __name__ == "__main__":
    unittest.main()
