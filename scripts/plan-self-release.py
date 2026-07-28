#!/usr/bin/env python3
"""Classify kin-actions main drift and resolve its highest release intent."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys


def is_release_affecting(path: str) -> bool:
    normalized = path.strip().replace("\\", "/")
    if not normalized or normalized in {"VERSION", "README.md", "CONTRIBUTING.md"}:
        return False
    if normalized.startswith(".kin-release/"):
        return True
    if normalized.startswith(".github/workflows/"):
        return True
    if normalized.startswith(".github/actions/"):
        return True
    if normalized.startswith("scripts/"):
        base = normalized.rsplit("/", 1)[-1]
        if base.startswith("test_") or base.endswith("_test.py"):
            return False
        return base.endswith((".py", ".sh"))
    return False


def parse_labels(raw: str) -> list[str]:
    return [item for item in re.split(r"[\s,]+", raw.strip()) if item]


def highest_intent(labels: list[str]) -> str:
    normalized = {label.lower() for label in labels}
    if "release:major" in normalized or "release/major" in normalized:
        return "major"
    if "release:minor" in normalized or "release/minor" in normalized:
        return "minor"
    return "patch"


def plan(paths: list[str], labels: list[str]) -> dict[str, object]:
    relevant = sorted({path for path in paths if is_release_affecting(path)})
    return {
        "release_needed": bool(relevant),
        "intent": highest_intent(labels),
        "release_paths": relevant,
    }


def _write_outputs(result: dict[str, object]) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return
    with open(output, "a", encoding="utf-8") as stream:
        stream.write(
            f"release_needed={'true' if result['release_needed'] else 'false'}\n"
        )
        stream.write(f"intent={result['intent']}\n")
        stream.write(
            "release_paths="
            + " ".join(str(path) for path in result["release_paths"])
            + "\n"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="plan a kin-actions self release")
    parser.add_argument("--labels", default="")
    parser.add_argument(
        "paths",
        nargs="*",
        help="changed paths; when omitted they are read one per line from stdin",
    )
    args = parser.parse_args(argv)
    paths = args.paths or [line.strip() for line in sys.stdin if line.strip()]
    result = plan(paths, parse_labels(args.labels))
    _write_outputs(result)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
