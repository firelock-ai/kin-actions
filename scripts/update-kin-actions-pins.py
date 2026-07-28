#!/usr/bin/env python3
"""Update exact kin-actions workflow pins in one allowlisted consumer checkout."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath


SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
PIN_RE = re.compile(
    r"(?P<prefix>uses:\s*"
    r"firelock-ai/kin-actions/\.github/workflows/"
    r"[A-Za-z0-9_.-]+\.ya?ml@v)"
    r"(?P<version>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*))"
)


class PinUpdateError(RuntimeError):
    """A manifest or consumer invariant failed."""


def parse_version(raw: str) -> tuple[int, int, int]:
    match = SEMVER_RE.fullmatch(raw.strip())
    if not match:
        raise PinUpdateError(f"expected stable numeric SemVer, got {raw!r}")
    return tuple(int(part) for part in match.groups())


def load_consumer_paths(manifest: Path, repository: str) -> list[str]:
    if manifest.is_symlink() or not manifest.is_file():
        raise PinUpdateError(f"manifest is not a regular file: {manifest}")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PinUpdateError(f"invalid consumer manifest: {exc}") from exc
    if data.get("schema") != 1 or not isinstance(data.get("repositories"), dict):
        raise PinUpdateError("consumer manifest must use schema 1")
    raw_paths = data["repositories"].get(repository)
    if not isinstance(raw_paths, list) or not raw_paths:
        raise PinUpdateError(f"repository is not allowlisted: {repository}")

    paths: list[str] = []
    for raw in raw_paths:
        if not isinstance(raw, str):
            raise PinUpdateError(f"{repository}: non-string path in manifest")
        pure = PurePosixPath(raw)
        if (
            pure.is_absolute()
            or ".." in pure.parts
            or not raw.startswith(".github/workflows/")
            or pure.suffix not in {".yml", ".yaml"}
        ):
            raise PinUpdateError(f"{repository}: unsafe workflow path {raw!r}")
        normalized = pure.as_posix()
        if normalized in paths:
            raise PinUpdateError(f"{repository}: duplicate workflow path {raw!r}")
        paths.append(normalized)
    return paths


def discover_pin_paths(root: Path) -> list[str]:
    """Find every live kin-actions reusable-workflow pin in a checkout."""

    root = root.resolve()
    workflow_root = root / ".github" / "workflows"
    if not workflow_root.exists():
        return []
    if workflow_root.is_symlink() or not workflow_root.is_dir():
        raise PinUpdateError(
            ".github/workflows is not a regular directory"
        )

    discovered: list[str] = []
    for directory, directories, filenames in os.walk(
        workflow_root, followlinks=False
    ):
        directory_path = Path(directory)
        for name in directories:
            candidate = directory_path / name
            if candidate.is_symlink():
                raise PinUpdateError(
                    "workflow tree contains a symlink directory: "
                    + str(candidate.relative_to(root))
                )
        for name in filenames:
            path = directory_path / name
            if path.suffix not in {".yml", ".yaml"}:
                continue
            if path.is_symlink() or not path.is_file():
                raise PinUpdateError(
                    "workflow tree contains a non-regular file: "
                    + str(path.relative_to(root))
                )
            if PIN_RE.search(path.read_text(encoding="utf-8")):
                discovered.append(path.relative_to(root).as_posix())
    return sorted(discovered)


def plan_updates(
    *,
    root: Path,
    paths: list[str],
    target_version: str,
) -> dict[Path, bytes]:
    root = root.resolve()
    target = parse_version(target_version)
    planned: dict[Path, bytes] = {}
    for relative in paths:
        path = (root / relative).absolute()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise PinUpdateError(f"allowlisted path escapes checkout: {relative}") from exc
        if path.is_symlink() or not path.is_file():
            raise PinUpdateError(f"allowlisted path is not a regular file: {relative}")
        text = path.read_text(encoding="utf-8")
        matches = list(PIN_RE.finditer(text))
        if not matches:
            raise PinUpdateError(
                f"allowlisted workflow has no exact kin-actions pin: {relative}"
            )
        for match in matches:
            current_raw = match.group("version")
            current = parse_version(current_raw)
            if current > target:
                raise PinUpdateError(
                    f"refusing to downgrade {relative} from v{current_raw} "
                    f"to v{target_version}"
                )
        updated = PIN_RE.sub(
            lambda match: match.group("prefix") + target_version,
            text,
        )
        if updated != text:
            planned[path] = updated.encode("utf-8")
    return planned


def update_pins(
    *,
    root: Path,
    manifest: Path,
    repository: str,
    target_version: str,
) -> dict[str, object]:
    parse_version(target_version)
    paths = load_consumer_paths(manifest, repository)
    discovered = discover_pin_paths(root)
    expected = sorted(paths)
    if discovered != expected:
        missing = sorted(set(discovered) - set(expected))
        stale = sorted(set(expected) - set(discovered))
        details = []
        if missing:
            details.append("unmanifested live pins: " + ", ".join(missing))
        if stale:
            details.append("manifest paths without live pins: " + ", ".join(stale))
        raise PinUpdateError(
            f"{repository}: consumer manifest is not exact; "
            + "; ".join(details)
        )
    planned = plan_updates(
        root=root,
        paths=paths,
        target_version=target_version,
    )
    # Every path and every candidate version has been validated before the
    # first write, so a mixed newer/older checkout cannot be partially updated.
    for path, data in planned.items():
        path.write_bytes(data)
    changed = sorted(str(path.relative_to(root.resolve())) for path in planned)
    return {
        "repository": repository,
        "version": target_version,
        "tag": f"v{target_version}",
        "changed": bool(changed),
        "changed_paths": changed,
        "allowed_paths": paths,
    }


def _write_outputs(result: dict[str, object]) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return
    with open(output, "a", encoding="utf-8") as stream:
        stream.write(f"changed={'true' if result['changed'] else 'false'}\n")
        stream.write(
            "changed_paths="
            + " ".join(str(item) for item in result["changed_paths"])
            + "\n"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="update allowlisted immutable kin-actions workflow pins"
    )
    parser.add_argument("--repository", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    try:
        result = update_pins(
            root=args.root,
            manifest=args.manifest.resolve(),
            repository=args.repository,
            target_version=args.version,
        )
    except PinUpdateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _write_outputs(result)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
