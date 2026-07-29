#!/usr/bin/env python3
"""Fail closed unless a caller Release workflow pins every external action."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Sequence


IMMUTABLE_EXTERNAL = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)?@"
    r"[0-9a-f]{40}$"
)


class WorkflowPinError(ValueError):
    """The workflow has an unsafe or unparseable uses surface."""


def semantic_uses(path: Path) -> list[tuple[int, str]]:
    extractor = Path(__file__).with_name("extract-workflow-uses.rb")
    try:
        completed = subprocess.run(
            ["ruby", str(extractor), str(path)],
            check=False,
            text=True,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise WorkflowPinError(
            "Ruby/Psych YAML parser is unavailable"
        ) from exc
    if completed.returncode != 0:
        raise WorkflowPinError(
            "semantic YAML parse failed: " + completed.stderr.strip()
        )
    try:
        raw = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise WorkflowPinError(
            "semantic YAML parser returned malformed JSON"
        ) from exc
    if not isinstance(raw, list):
        raise WorkflowPinError("semantic YAML parser must return one list")
    values: list[tuple[int, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise WorkflowPinError("semantic uses entry must be one object")
        line = item.get("line")
        value = item.get("value")
        if (
            not isinstance(line, int)
            or isinstance(line, bool)
            or line < 1
            or not isinstance(value, str)
        ):
            raise WorkflowPinError("semantic uses entry is malformed")
        values.append((line, value))
    return values


def validate_workflow(path: Path) -> int:
    if not path.is_file():
        raise WorkflowPinError(f"Release workflow is missing: {path}")
    if path.is_symlink():
        raise WorkflowPinError(f"Release workflow must not be a symlink: {path}")
    uses = semantic_uses(path)
    for line_number, value in uses:
        if value.startswith("./"):
            raise WorkflowPinError(
                f"line {line_number}: local action/workflow {value!r} "
                "is not allowed in the Release workflow because transitive "
                "external uses cannot be proven here"
            )
        if not IMMUTABLE_EXTERNAL.fullmatch(value):
            raise WorkflowPinError(
                f"line {line_number}: external action {value!r} is not "
                "pinned to one lowercase 40-hex commit SHA"
            )
    return len(uses)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        uses_count = validate_workflow(args.workflow)
    except WorkflowPinError as exc:
        print(f"Release workflow pin validation refused: {exc}", file=sys.stderr)
        return 1
    print(
        f"Release workflow pin validation passed: {args.workflow} "
        f"({uses_count} uses surfaces)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
