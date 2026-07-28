"""Regression tests for bounded downstream release dispatch."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


SCRIPT = Path(__file__).with_name("dispatch-downstreams.sh")


class _DispatchServer:
    def __init__(self, statuses: list[int]):
        self.statuses = statuses
        self.requests: list[dict[str, object]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                outer.requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "body": json.loads(body),
                    }
                )
                index = min(len(outer.requests) - 1, len(outer.statuses) - 1)
                status = outer.statuses[index]
                self.send_response(status)
                if status == 429:
                    self.send_header("Retry-After", "0.001")
                self.end_headers()
                if status >= 400:
                    self.wfile.write(b"temporary test response")

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "_DispatchServer":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"


class DispatchIntegration(unittest.TestCase):
    def run_dispatch(
        self, statuses: list[int], *, repo: str = "firelock-ai/kin-db"
    ) -> tuple[subprocess.CompletedProcess[str], list[dict[str, object]]]:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "downstreams.json"
            manifest.write_text(json.dumps({"downstreams": [{"repo": repo}]}))
            with _DispatchServer(statuses) as server:
                env = {
                    **os.environ,
                    "PACKAGE": "kin-model",
                    "VERSION": "0.7.0",
                    "GITHUB_REPOSITORY": "firelock-ai/kin-model",
                    "GITHUB_SHA": "a" * 40,
                    "DOWNSTREAM_MANIFEST": str(manifest),
                    "KIN_DOWNSTREAM_DISPATCH_TOKEN": "test-token",
                    "KIN_GITHUB_API_URL": server.url,
                    "KIN_DISPATCH_MAX_ATTEMPTS": "4",
                    "KIN_DISPATCH_BASE_DELAY_SECONDS": "0.001",
                    "KIN_DISPATCH_MAX_DELAY_SECONDS": "0.001",
                    "KIN_DISPATCH_TIMEOUT_SECONDS": "2",
                    "RELEASE_TAG_STATUS": "already-present",
                }
                result = subprocess.run(
                    ["bash", str(SCRIPT)],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                return result, list(server.requests)

    def test_retries_429_and_5xx_then_succeeds(self) -> None:
        result, requests = self.run_dispatch([429, 503, 204])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(requests), 3)
        self.assertEqual({json.dumps(row["body"], sort_keys=True) for row in requests}, {
            json.dumps(
                {
                    "event_type": "kin-registry-release",
                    "client_payload": {
                        "crate_name": "kin-model",
                        "crate_version": "0.7.0",
                        "source_repo": "firelock-ai/kin-model",
                        "source_sha": "a" * 40,
                    },
                },
                sort_keys=True,
            )
        })
        self.assertTrue(
            all(row["authorization"] == "Bearer test-token" for row in requests)
        )
        self.assertIn("registry publication and fresh-consumer proof are complete", result.stdout)
        self.assertIn("release-tag status=already-present", result.stdout)
        self.assertIn("attempts=3", result.stdout)

    def test_non_transient_failure_is_not_retried(self) -> None:
        result, requests = self.run_dispatch([422, 204])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(requests), 1)
        self.assertIn("HTTP 422", result.stderr)
        self.assertIn("Registry publication is already durable", result.stderr)

    def test_transient_failures_stop_at_bound(self) -> None:
        result, requests = self.run_dispatch([500, 502, 503, 504, 204])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(requests), 4)
        self.assertIn("failed after 4 attempts", result.stderr)

    def test_invalid_repository_fails_before_network(self) -> None:
        result, requests = self.run_dispatch([204], repo="../../elsewhere")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(requests, [])
        self.assertIn("invalid downstream repository", result.stderr)


if __name__ == "__main__":
    unittest.main()
