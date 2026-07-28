"""Regression tests for bounded downstream release dispatch."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


SCRIPT = Path(__file__).with_name("dispatch-downstreams.sh")
PYTHON_SCRIPT = Path(__file__).with_name("dispatch-downstreams.py")
SPEC = importlib.util.spec_from_file_location("dispatch_downstreams", PYTHON_SCRIPT)
assert SPEC is not None and SPEC.loader is not None
dispatch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dispatch
SPEC.loader.exec_module(dispatch)


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
                if status in (403, 429):
                    self.send_header("Retry-After", "0.001")
                self.end_headers()
                if status >= 400:
                    body = (
                        b"secondary rate limit test response"
                        if status == 403
                        else b"temporary test response"
                    )
                    self.wfile.write(body)

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
                    "KIN_DISPATCH_MAX_RETRY_WAIT_SECONDS": "0.1",
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
                        "delivery_id": (
                            f"firelock-ai/kin-model@{'a' * 40}:kin-model@0.7.0"
                        ),
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

    def test_retries_rate_limited_403_then_succeeds(self) -> None:
        result, requests = self.run_dispatch([403, 204])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(requests), 2)
        self.assertIn("HTTP 403", result.stdout)

    def test_retry_after_is_not_shortened_to_local_backoff_cap(self) -> None:
        self.assertEqual(
            dispatch._retry_after_seconds(
                {"Retry-After": "120"},
                attempt=1,
                base_delay=1,
                max_delay=15,
                jitter=lambda: 0,
            ),
            120,
        )

    def test_long_server_delay_fails_without_early_retry(self) -> None:
        with self.assertRaisesRegex(dispatch.DispatchError, "refusing to retry early"):
            dispatch._consume_retry_budget(
                repo="firelock-ai/kin-db",
                delay=120,
                waited=0,
                retry_wait_budget=30,
            )

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
