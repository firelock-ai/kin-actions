#!/usr/bin/env python3
"""Send bounded, retryable Kin registry release notifications."""

from __future__ import annotations

import email.utils
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping


SUCCESS_STATUSES = frozenset({200, 201, 202, 204})
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BASE_DELAY_SECONDS = 1.0
DEFAULT_MAX_DELAY_SECONDS = 15.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 20.0


class DispatchError(RuntimeError):
    """A downstream notification could not be delivered."""


def _positive_number(name: str, default: float, *, integer: bool = False) -> float | int:
    raw = os.environ.get(name)
    if raw is None:
        return int(default) if integer else default
    try:
        value = int(raw) if integer else float(raw)
    except ValueError as exc:
        raise DispatchError(f"{name} must be a positive number, got {raw!r}") from exc
    if value <= 0:
        raise DispatchError(f"{name} must be greater than zero, got {raw!r}")
    return value


def _retry_after_seconds(
    headers: Mapping[str, str],
    *,
    attempt: int,
    base_delay: float,
    max_delay: float,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    jitter: Callable[[], float] = random.random,
) -> float:
    retry_after = headers.get("Retry-After")
    if retry_after:
        try:
            requested = float(retry_after)
        except ValueError:
            try:
                parsed = email.utils.parsedate_to_datetime(retry_after)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                requested = max(0.0, (parsed - now()).total_seconds())
            except (TypeError, ValueError, OverflowError):
                requested = 0.0
        if requested > 0:
            return min(requested, max_delay)

    exponential = min(base_delay * (2 ** (attempt - 1)), max_delay)
    # Small positive jitter prevents synchronized dependency waves while
    # preserving the hard upper bound.
    return min(exponential * (0.8 + (0.2 * jitter())), max_delay)


def _is_retryable_status(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


def _response_excerpt(error: urllib.error.HTTPError) -> str:
    try:
        body = error.read(512).decode("utf-8", errors="replace").strip()
    except (OSError, ValueError):
        body = ""
    return f": {body}" if body else ""


def _dispatch_one(
    *,
    api_url: str,
    repo: str,
    payload: bytes,
    token: str,
    max_attempts: int,
    base_delay: float,
    max_delay: float,
    timeout: float,
    opener: Callable[..., object] = urllib.request.urlopen,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    for attempt in range(1, max_attempts + 1):
        request = urllib.request.Request(
            f"{api_url}/repos/{repo}/dispatches",
            data=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with opener(request, timeout=timeout) as response:
                status = int(response.status)
                if status not in SUCCESS_STATUSES:
                    raise DispatchError(
                        f"dispatch to {repo} failed: unexpected HTTP {status}"
                    )
                return attempt
        except urllib.error.HTTPError as error:
            status = int(error.code)
            if not _is_retryable_status(status):
                raise DispatchError(
                    f"dispatch to {repo} failed: HTTP {status}"
                    f"{_response_excerpt(error)}"
                ) from error
            if attempt == max_attempts:
                raise DispatchError(
                    f"dispatch to {repo} failed after {max_attempts} attempts: "
                    f"HTTP {status}{_response_excerpt(error)}"
                ) from error
            delay = _retry_after_seconds(
                error.headers,
                attempt=attempt,
                base_delay=base_delay,
                max_delay=max_delay,
            )
            print(
                f"::warning title=Transient downstream dispatch failure::"
                f"{repo} returned HTTP {status}; retrying attempt "
                f"{attempt + 1}/{max_attempts} in {delay:.1f}s",
                flush=True,
            )
            sleeper(delay)
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt == max_attempts:
                raise DispatchError(
                    f"dispatch to {repo} failed after {max_attempts} attempts: {error}"
                ) from error
            delay = _retry_after_seconds(
                {},
                attempt=attempt,
                base_delay=base_delay,
                max_delay=max_delay,
            )
            print(
                f"::warning title=Transient downstream dispatch failure::"
                f"{repo} request failed ({error}); retrying attempt "
                f"{attempt + 1}/{max_attempts} in {delay:.1f}s",
                flush=True,
            )
            sleeper(delay)
    raise AssertionError("bounded dispatch loop exhausted without returning")


def dispatch_manifest(
    manifest: Path,
    package: str,
    version: str,
    source_repo: str,
    source_sha: str,
) -> None:
    token = os.environ["KIN_DOWNSTREAM_DISPATCH_TOKEN"]
    api_url = os.environ.get("KIN_GITHUB_API_URL", "https://api.github.com").rstrip("/")
    max_attempts = int(
        _positive_number(
            "KIN_DISPATCH_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS, integer=True
        )
    )
    base_delay = float(
        _positive_number(
            "KIN_DISPATCH_BASE_DELAY_SECONDS", DEFAULT_BASE_DELAY_SECONDS
        )
    )
    max_delay = float(
        _positive_number("KIN_DISPATCH_MAX_DELAY_SECONDS", DEFAULT_MAX_DELAY_SECONDS)
    )
    timeout = float(
        _positive_number(
            "KIN_DISPATCH_TIMEOUT_SECONDS", DEFAULT_REQUEST_TIMEOUT_SECONDS
        )
    )

    with manifest.open(encoding="utf-8") as stream:
        data = json.load(stream)
    rows = data.get("downstreams", []) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise DispatchError("downstream manifest must be a list or contain a downstreams list")

    tag_status = os.environ.get("RELEASE_TAG_STATUS", "not-requested")
    print(
        "release delivery checkpoint: registry publication and fresh-consumer "
        f"proof are complete; release-tag status={tag_status}; "
        "downstream notifications pending"
    )

    for row in rows:
        repo = row.get("repo") if isinstance(row, dict) else row
        if not isinstance(repo, str) or not REPO_PATTERN.fullmatch(repo):
            raise DispatchError(f"invalid downstream repository: {repo!r}")
        body = json.dumps(
            {
                "event_type": "kin-registry-release",
                "client_payload": {
                    "crate_name": package,
                    "crate_version": version,
                    "source_repo": source_repo,
                    "source_sha": source_sha,
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        attempts = _dispatch_one(
            api_url=api_url,
            repo=repo,
            payload=body,
            token=token,
            max_attempts=max_attempts,
            base_delay=base_delay,
            max_delay=max_delay,
            timeout=timeout,
        )
        print(f"dispatched {package}@{version} to {repo} (attempts={attempts})")


def main(argv: list[str]) -> int:
    if len(argv) != 6:
        print(
            "usage: dispatch-downstreams.py "
            "<manifest> <package> <version> <source-repo> <source-sha>",
            file=sys.stderr,
        )
        return 2
    try:
        dispatch_manifest(Path(argv[1]), *argv[2:])
    except (DispatchError, OSError, json.JSONDecodeError, KeyError) as error:
        print(
            "::error title=Downstream dispatch incomplete::"
            f"{error}. Registry publication is already durable; "
            "rerun only the failed dispatch stage.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
