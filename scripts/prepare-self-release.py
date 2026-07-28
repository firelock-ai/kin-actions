#!/usr/bin/env python3
"""Prepare kin-actions' own generated VERSION and documentation bytes."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
PIN_RE = re.compile(
    r"(?P<prefix>firelock-ai/kin-actions/\.github/workflows/"
    r"[A-Za-z0-9_.-]+\.ya?ml@v)"
    r"(?P<version>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*))"
)


class SelfReleaseError(RuntimeError):
    """A self-release invariant failed."""


def parse_version(raw: str) -> tuple[int, int, int]:
    match = SEMVER_RE.fullmatch(raw.strip())
    if not match:
        raise SelfReleaseError(
            f"automatic releases require stable numeric SemVer, got {raw!r}"
        )
    return tuple(int(part) for part in match.groups())


def format_version(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def successor(
    version: tuple[int, int, int], intent: str
) -> tuple[int, int, int]:
    major, minor, patch = version
    if intent == "patch":
        return major, minor, patch + 1
    if intent == "minor":
        return major, minor + 1, 0
    if intent == "major":
        return major + 1, 0, 0
    raise SelfReleaseError(f"invalid release intent: {intent!r}")


def choose_target(base_raw: str, current_raw: str, intent: str) -> str:
    base = parse_version(base_raw)
    current = parse_version(current_raw)
    requested = successor(base, intent)
    valid = {successor(base, level) for level in ("patch", "minor", "major")}
    if current == base:
        chosen = requested
    elif current not in valid:
        raise SelfReleaseError(
            f"current VERSION {current_raw} is neither base {base_raw} nor an "
            "automatic successor"
        )
    else:
        chosen = max(current, requested)
    return format_version(chosen)


def _regular_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise SelfReleaseError(f"generated input is not a regular file: {path}")


def prepare_self_release(
    *,
    root: Path,
    base_version: str,
    intent: str,
    version_file: str = "VERSION",
    docs: tuple[str, ...] = ("README.md", "CONTRIBUTING.md"),
) -> dict[str, object]:
    root = root.resolve()
    version_path = (root / version_file).absolute()
    try:
        version_path.relative_to(root)
    except ValueError as exc:
        raise SelfReleaseError("VERSION path escapes repository root") from exc
    _regular_file(version_path)
    current_raw = version_path.read_text(encoding="utf-8").strip()
    target = choose_target(base_version, current_raw, intent)
    target_tuple = parse_version(target)

    paths = [version_path]
    for relative in docs:
        path = (root / relative).absolute()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise SelfReleaseError(f"doc path escapes repository root: {relative}") from exc
        _regular_file(path)
        paths.append(path)

    originals = {path: path.read_bytes() for path in paths}
    planned: dict[Path, bytes] = {version_path: (target + "\n").encode("utf-8")}
    for path in paths[1:]:
        text = path.read_text(encoding="utf-8")
        for match in PIN_RE.finditer(text):
            found = match.group("version")
            if parse_version(found) > target_tuple:
                raise SelfReleaseError(
                    f"refusing to downgrade {path.relative_to(root)} from "
                    f"v{found} to v{target}"
                )
        updated = PIN_RE.sub(lambda match: match.group("prefix") + target, text)
        planned[path] = updated.encode("utf-8")

    for path, data in planned.items():
        path.write_bytes(data)

    changed = sorted(
        str(path.relative_to(root))
        for path, original in originals.items()
        if path.read_bytes() != original
    )
    chosen_tuple = parse_version(target)
    base_tuple = parse_version(base_version)
    chosen_intent = next(
        level
        for level in ("major", "minor", "patch")
        if successor(base_tuple, level) == chosen_tuple
    )
    return {
        "base_version": base_version,
        "previous_generated_version": current_raw,
        "target_version": target,
        "tag": f"v{target}",
        "intent": chosen_intent,
        "generated_paths": changed,
        "allowed_paths": sorted(str(path.relative_to(root)) for path in paths),
    }


def _write_outputs(result: dict[str, object]) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return
    with open(output, "a", encoding="utf-8") as stream:
        for key in (
            "base_version",
            "previous_generated_version",
            "target_version",
            "tag",
            "intent",
        ):
            stream.write(f"{key}={result[key]}\n")
        stream.write(
            "generated_paths="
            + " ".join(str(path) for path in result["generated_paths"])
            + "\n"
        )
        stream.write(
            "allowed_paths="
            + " ".join(str(path) for path in result["allowed_paths"])
            + "\n"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="prepare kin-actions' own automatic release bytes"
    )
    parser.add_argument("--base-version", required=True)
    parser.add_argument("--intent", choices=("patch", "minor", "major"), default="patch")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--version-file", default="VERSION")
    parser.add_argument("--doc", action="append", default=[])
    args = parser.parse_args(argv)
    docs = tuple(args.doc) if args.doc else ("README.md", "CONTRIBUTING.md")
    try:
        result = prepare_self_release(
            root=args.root,
            base_version=args.base_version,
            intent=args.intent,
            version_file=args.version_file,
            docs=docs,
        )
    except SelfReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _write_outputs(result)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
