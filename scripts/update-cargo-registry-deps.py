#!/usr/bin/env python3
"""Transactionally update Kin registry requirements in Cargo manifests.

Exit status 0 means at least one requirement changed (or would change under
``--dry-run``), 2 means every requested requirement was already current or
absent, and every other status is a hard failure.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence


_CORE_IDENTIFIER = r"(?:0|[1-9]\d*)"
_PRERELEASE_IDENTIFIER = (
    r"(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
)
_BUILD_IDENTIFIER = r"[0-9A-Za-z-]+"
_SIMPLE_REQUIREMENT_VERSION = (
    rf"{_CORE_IDENTIFIER}"
    rf"(?:\.{_CORE_IDENTIFIER}"
    rf"(?:\.{_CORE_IDENTIFIER}"
    rf"(?:-{_PRERELEASE_IDENTIFIER}"
    rf"(?:\.{_PRERELEASE_IDENTIFIER})*)?"
    rf"(?:\+{_BUILD_IDENTIFIER}(?:\.{_BUILD_IDENTIFIER})*)?"
    r")?"
    r")?"
)
_SIMPLE_REQUIREMENT = re.compile(
    rf"^(?P<operator>[=~^]?)(?P<spacing>\s*)"
    rf"(?P<version>{_SIMPLE_REQUIREMENT_VERSION})$"
)
_SEMVER = re.compile(
    rf"^(?P<major>{_CORE_IDENTIFIER})\."
    rf"(?P<minor>{_CORE_IDENTIFIER})\."
    rf"(?P<patch>{_CORE_IDENTIFIER})"
    rf"(?:-(?P<prerelease>{_PRERELEASE_IDENTIFIER}"
    rf"(?:\.{_PRERELEASE_IDENTIFIER})*))?"
    rf"(?:\+(?P<build>{_BUILD_IDENTIFIER}"
    rf"(?:\.{_BUILD_IDENTIFIER})*))?$"
)
_TABLE_VERSION = re.compile(
    r"""^\s*(?P<quote>["'])(?P<requirement>[^"']*)(?P=quote)"""
    r"""\s*(?:#.*)?$"""
)
_DEPENDENCY_TABLES = {"dependencies", "dev-dependencies", "build-dependencies"}
_PROBE_KEY = "__kin_dependency_wave_probe__"


class UpdateError(RuntimeError):
    """A dependency wave cannot proceed without risking a partial update."""


@dataclass(frozen=True)
class DependencyTarget:
    path: tuple[str, ...]
    crate: str
    requirement: str
    updated_requirement: str


@dataclass(frozen=True)
class ManifestPlan:
    path: Path
    before: bytes
    after: bytes
    changed_crates: tuple[str, ...]


def sparse_index_path(name: str) -> str:
    name = name.lower()
    if len(name) == 1:
        return f"1/{name}"
    if len(name) == 2:
        return f"2/{name}"
    if len(name) == 3:
        return f"3/{name[0]}/{name}"
    return f"{name[:2]}/{name[2:4]}/{name}"


def parse_version(version: str) -> tuple[object, ...]:
    """Return a SemVer precedence key, ignoring build metadata."""

    match = _SEMVER.fullmatch(version)
    if match is None:
        raise UpdateError(f"registry returned invalid SemVer: {version!r}")
    prerelease = match.group("prerelease")
    identifiers: tuple[tuple[int, object], ...] = ()
    if prerelease is not None:
        identifiers = tuple(
            (0, int(identifier))
            if identifier.isdigit()
            else (1, identifier)
            for identifier in prerelease.split(".")
        )
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        prerelease is None,
        identifiers,
    )


def latest_version(registry_url: str, crate: str) -> str | None:
    url = f"{registry_url.rstrip('/')}/registry/cargo/{sparse_index_path(crate)}"
    try:
        body = urllib.request.urlopen(url, timeout=10).read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    versions = []
    for line in body.splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if not obj.get("yanked", False):
            versions.append(obj["vers"])
    return max(versions, key=parse_version) if versions else None


def update_requirement(requirement: str, version: str) -> str:
    """Move a simple Cargo requirement forward without changing its operator."""

    if _SEMVER.fullmatch(version) is None:
        raise UpdateError(f"unsupported target version: {version!r}")
    match = _SIMPLE_REQUIREMENT.fullmatch(requirement)
    if match is None:
        raise UpdateError(
            f"unsupported Cargo requirement {requirement!r}; "
            "dependency-wave updates require a simple bare, =, ~, or ^ version"
        )
    current = match.group("version")
    current_core, separator, current_suffix = current.partition("-")
    if not separator:
        current_core, separator, current_suffix = current.partition("+")
        suffix = f"+{current_suffix}" if separator else ""
    else:
        suffix = f"-{current_suffix}"
    current_parts = current_core.split(".")
    current_floor = ".".join(current_parts + (["0"] * (3 - len(current_parts))))
    current_floor += suffix
    if parse_version(current_floor) > parse_version(version):
        return requirement
    return f"{match.group('operator')}{match.group('spacing')}{version}"


def _is_dependency_table(path: tuple[str, ...]) -> bool:
    if len(path) == 1:
        return path[0] in _DEPENDENCY_TABLES
    if path == ("workspace", "dependencies"):
        return True
    return (
        len(path) == 3
        and path[0] == "target"
        and path[2] in _DEPENDENCY_TABLES
    )


def _collect_targets(
    document: Mapping[str, object],
    versions: Mapping[str, str],
    manifest: Path,
) -> list[DependencyTarget]:
    targets: list[DependencyTarget] = []

    def visit(value: Mapping[str, object], path: tuple[str, ...]) -> None:
        if _is_dependency_table(path):
            for alias, spec in value.items():
                if not isinstance(spec, Mapping):
                    # String shorthand cannot carry a registry, so it is not a
                    # Kin registry declaration and is intentionally ignored.
                    continue
                package = spec.get("package", alias)
                registry = spec.get("registry")
                if registry != "kin":
                    continue
                if not isinstance(package, str):
                    raise UpdateError(
                        f"{manifest}: dependency {'.'.join(path + (alias,))} "
                        "has a non-string package name"
                    )
                if package not in versions:
                    continue
                requirement = spec.get("version")
                if not isinstance(requirement, str):
                    raise UpdateError(
                        f"{manifest}: Kin registry dependency "
                        f"{'.'.join(path + (alias,))} has no string version"
                    )
                targets.append(
                    DependencyTarget(
                        path=path + (alias,),
                        crate=package,
                        requirement=requirement,
                        updated_requirement=update_requirement(
                            requirement, versions[package]
                        ),
                    )
                )
            return

        for key, child in value.items():
            if isinstance(child, Mapping):
                visit(child, path + (key,))

    visit(document, ())
    return targets


def _without_comment(line: str) -> str:
    """Remove a TOML comment while respecting single- and double-quoted strings."""

    quote: str | None = None
    escaped = False
    for index, char in enumerate(line):
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif quote == "'":
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#":
            return line[:index]
    return line


def _has_multiline_string(text: str) -> bool:
    """Conservatively identify physical multiline TOML string syntax.

    The standard parser validates the semantic document but does not expose
    source spans. Until it does, physical multiline strings are rejected when
    a requested Kin dependency is present so string contents can never be
    mistaken for table headers or assignments by the formatting-preserving
    source mapper.
    """

    return any(
        delimiter in _without_comment(line)
        for line in text.splitlines()
        for delimiter in ('"""', "'''")
    )


def _find_unquoted_equal(line: str) -> int | None:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(line):
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif quote == "'":
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "=":
            return index
    return None


def _find_probe_path(value: object, path: tuple[str, ...] = ()) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        raise UpdateError("internal TOML key-path probe was not a table")
    if value.get(_PROBE_KEY) is True:
        return path
    matches = [
        _find_probe_path(child, path + (key,))
        for key, child in value.items()
        if isinstance(child, Mapping)
    ]
    matches = [match for match in matches if match]
    if len(matches) != 1:
        raise UpdateError("could not determine TOML key path")
    return matches[0]


def _parse_header_path(header: str) -> tuple[str, ...]:
    try:
        document = tomllib.loads(f"{header}\n{_PROBE_KEY} = true\n")
    except tomllib.TOMLDecodeError as exc:
        raise UpdateError(f"unsupported TOML table header {header!r}: {exc}") from exc
    return _find_probe_path(document)


def _parse_key_path(key: str) -> tuple[str, ...]:
    try:
        document = tomllib.loads(f"{key} = {{ {_PROBE_KEY} = true }}\n")
    except tomllib.TOMLDecodeError as exc:
        raise UpdateError(f"unsupported TOML dependency key {key!r}: {exc}") from exc
    return _find_probe_path(document)


def _inline_version_span(
    value: str,
    manifest: Path,
    target: DependencyTarget,
) -> tuple[int, int, str]:
    opening = len(value) - len(value.lstrip())
    if opening >= len(value) or value[opening] != "{":
        raise UpdateError(
            f"{manifest}: unsupported inline-table formatting for "
            f"{'.'.join(target.path)}"
        )

    segments: list[tuple[int, int]] = []
    segment_start = opening + 1
    stack = ["{"]
    quote: str | None = None
    escaped = False
    closing: int | None = None
    for index in range(opening + 1, len(value)):
        char = value[index]
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if quote == "'":
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char in "[{":
            stack.append(char)
        elif char == "]":
            if stack[-1] != "[":
                raise UpdateError(
                    f"{manifest}: malformed inline table for {'.'.join(target.path)}"
                )
            stack.pop()
        elif char == "}":
            if stack[-1] != "{":
                raise UpdateError(
                    f"{manifest}: malformed inline table for {'.'.join(target.path)}"
                )
            if len(stack) == 1:
                segments.append((segment_start, index))
                closing = index
                break
            stack.pop()
        elif char == "," and len(stack) == 1:
            segments.append((segment_start, index))
            segment_start = index + 1

    if closing is None or _without_comment(value[closing + 1 :]).strip():
        raise UpdateError(
            f"{manifest}: unsupported inline-table formatting for "
            f"{'.'.join(target.path)}"
        )

    matches: list[tuple[int, int, str]] = []
    for start, end in segments:
        segment = value[start:end]
        equals = _find_unquoted_equal(segment)
        if equals is None:
            continue
        key = segment[:equals].strip()
        if _parse_key_path(key) != ("version",):
            continue
        version_value = segment[equals + 1 :]
        matcher = _TABLE_VERSION.fullmatch(version_value)
        if matcher is None:
            raise UpdateError(
                f"{manifest}: unsupported inline-table version formatting for "
                f"{'.'.join(target.path)}"
            )
        matches.append(
            (
                start + equals + 1 + matcher.start("requirement"),
                start + equals + 1 + matcher.end("requirement"),
                matcher.group("requirement"),
            )
        )

    if len(matches) != 1:
        raise UpdateError(
            f"{manifest}: expected one top-level inline version for "
            f"{'.'.join(target.path)}, found {len(matches)}"
        )
    return matches[0]


def _replacement_span(
    line: str,
    line_offset: int,
    equals: int,
    target: DependencyTarget,
    table_form: bool,
    manifest: Path,
) -> tuple[int, int, str]:
    value = line[equals + 1 :].rstrip("\r\n")
    if table_form:
        matcher = _TABLE_VERSION.fullmatch(value)
        if matcher is None:
            raise UpdateError(
                f"{manifest}: unsupported table formatting for "
                f"{'.'.join(target.path)}; use a single-quoted or "
                "double-quoted simple requirement on one line"
            )
        relative_start = matcher.start("requirement")
        relative_end = matcher.end("requirement")
        raw_requirement = matcher.group("requirement")
    else:
        relative_start, relative_end, raw_requirement = _inline_version_span(
            value, manifest, target
        )
    if raw_requirement != target.requirement:
        raise UpdateError(
            f"{manifest}: escaped or ambiguous version literal for "
            f"{'.'.join(target.path)} is unsupported"
        )
    start = line_offset + equals + 1 + relative_start
    end = line_offset + equals + 1 + relative_end
    return start, end, target.updated_requirement


def _rewrite_manifest(
    path: Path,
    text: str,
    versions: Mapping[str, str],
) -> tuple[str, tuple[str, ...]]:
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise UpdateError(f"{path}: malformed TOML: {exc}") from exc

    targets = _collect_targets(document, versions, path)
    if not targets:
        return text, ()
    if _has_multiline_string(text):
        raise UpdateError(
            f"{path}: physical multiline TOML strings are unsupported during "
            "dependency-wave rewriting; convert them to ordinary strings or "
            "update the dependency manually"
        )

    by_path = {target.path: target for target in targets}
    if len(by_path) != len(targets):
        raise UpdateError(f"{path}: ambiguous duplicate Kin dependency declarations")

    locations: dict[tuple[str, ...], list[tuple[int, int, str]]] = {
        target.path: [] for target in targets
    }
    current_table: tuple[str, ...] | None = ()
    inline_tables = {target.path[:-1] for target in targets}
    dependency_tables = {target.path for target in targets}
    offset = 0
    for line in text.splitlines(keepends=True):
        code = _without_comment(line).strip()
        if code.startswith("[["):
            current_table = None
        elif code.startswith("[") and code.endswith("]"):
            current_table = _parse_header_path(code)
        elif code and current_table is not None:
            equals = _find_unquoted_equal(line)
            if equals is not None:
                key = line[:equals].strip()
                if (
                    current_table not in inline_tables
                    and current_table not in dependency_tables
                    and not (
                        current_table == ()
                        and any(
                            table_name in key
                            for table_name in _DEPENDENCY_TABLES
                        )
                    )
                ):
                    offset += len(line)
                    continue
                relative_path = _parse_key_path(key)
                absolute_path = current_table + relative_path
                if absolute_path in by_path:
                    target = by_path[absolute_path]
                    locations[target.path].append(
                        _replacement_span(
                            line, offset, equals, target, False, path
                        )
                    )
                elif (
                    len(absolute_path) > 1
                    and absolute_path[-1] == "version"
                    and absolute_path[:-1] in by_path
                ):
                    target = by_path[absolute_path[:-1]]
                    locations[target.path].append(
                        _replacement_span(
                            line, offset, equals, target, True, path
                        )
                    )
        offset += len(line)

    replacements: list[tuple[int, int, str]] = []
    changed_crates: set[str] = set()
    for target in targets:
        found = locations[target.path]
        if len(found) != 1:
            raise UpdateError(
                f"{path}: expected one writable version for "
                f"{'.'.join(target.path)}, found {len(found)}; unsupported "
                "dependency declaration shape"
            )
        start, end, replacement = found[0]
        if target.requirement != replacement:
            replacements.append((start, end, replacement))
            changed_crates.add(target.crate)

    rewritten = text
    for start, end, replacement in sorted(replacements, reverse=True):
        rewritten = rewritten[:start] + replacement + rewritten[end:]
    return rewritten, tuple(sorted(changed_crates))


def plan_manifests(
    manifests: Iterable[Path | str],
    versions: Mapping[str, str],
) -> list[ManifestPlan]:
    """Parse and plan every manifest before returning any writable state."""

    plans: list[ManifestPlan] = []
    seen: set[Path] = set()
    for supplied_path in manifests:
        path = Path(supplied_path)
        if not path.exists():
            raise UpdateError(f"manifest does not exist: {path}")
        if path.is_symlink():
            raise UpdateError(f"symlinked manifests are unsupported: {path}")
        identity = path.resolve()
        if identity in seen:
            continue
        seen.add(identity)
        try:
            before = path.read_bytes()
            text = before.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise UpdateError(f"cannot read UTF-8 manifest {path}: {exc}") from exc
        after_text, changed_crates = _rewrite_manifest(path, text, versions)
        plans.append(
            ManifestPlan(
                path=path,
                before=before,
                after=after_text.encode("utf-8"),
                changed_crates=changed_crates,
            )
        )
    return plans


def _atomic_write(path: Path, content: bytes, mode: int | None = None) -> None:
    if mode is None:
        mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.dependency-wave-", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _restore(snapshots: Mapping[Path, tuple[bytes, int]]) -> list[str]:
    failures = []
    for path, (content, mode) in snapshots.items():
        try:
            _atomic_write(path, content, mode)
        except OSError as exc:
            failures.append(f"{path}: {exc}")
    return failures


def apply_plans(
    plans: Sequence[ManifestPlan],
    versions: Mapping[str, str],
    *,
    dry_run: bool = False,
    lock_path: Path | str = Path("Cargo.lock"),
    cargo_run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[tuple[Path, str, str]]:
    changes = [
        (plan.path, crate, versions[crate])
        for plan in plans
        for crate in plan.changed_crates
    ]
    if dry_run or not changes:
        return changes

    changed_plans = [plan for plan in plans if plan.before != plan.after]
    lock_path = Path(lock_path)
    for plan in changed_plans:
        try:
            current = plan.path.read_bytes()
        except OSError as exc:
            raise UpdateError(
                f"cannot verify manifest precondition {plan.path}: {exc}"
            ) from exc
        if current != plan.before:
            raise UpdateError(
                f"manifest changed after planning; refusing to overwrite {plan.path}"
            )
    snapshots = {
        plan.path: (plan.before, stat.S_IMODE(plan.path.stat().st_mode))
        for plan in changed_plans
    }
    lock_exists = lock_path.exists()
    if lock_exists:
        snapshots[lock_path] = (
            lock_path.read_bytes(),
            stat.S_IMODE(lock_path.stat().st_mode),
        )

    try:
        for plan in changed_plans:
            _atomic_write(plan.path, plan.after)
        if lock_exists:
            changed_crates = {crate for _, crate, _ in changes}
            for crate, version in versions.items():
                if crate in changed_crates:
                    cargo_run(
                        ["cargo", "update", "-p", crate, "--precise", version],
                        check=True,
                    )
    except (OSError, subprocess.SubprocessError) as exc:
        rollback_failures = _restore(snapshots)
        rollback = (
            f"; rollback failures: {', '.join(rollback_failures)}"
            if rollback_failures
            else "; manifests and Cargo.lock restored"
        )
        raise UpdateError(f"dependency update failed: {exc}{rollback}") from exc
    return changes


def update_manifest(
    path: str,
    crate: str,
    version: str,
    dry_run: bool = False,
) -> bool:
    """Compatibility wrapper for callers updating one manifest without a lockfile."""

    versions = {crate: version}
    plans = plan_manifests([path], versions)
    changes = apply_plans(
        plans,
        versions,
        dry_run=dry_run,
        lock_path=Path(path).with_name("__kin_no_lockfile__"),
    )
    return bool(changes)


def _resolve_versions(
    crates: Sequence[str],
    requested_version: str,
    registry_url: str,
) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for crate in dict.fromkeys(crates):
        requested = requested_version or None
        if requested is not None and _SEMVER.fullmatch(requested) is None:
            raise UpdateError(f"unsupported target version for {crate}: {requested!r}")
        registry_latest = latest_version(registry_url, crate)
        candidates = [candidate for candidate in (requested, registry_latest) if candidate]
        version = max(candidates, key=parse_version) if candidates else None
        if not version:
            print(f"no published version found for {crate}; skipping")
            continue
        if requested and registry_latest and version != requested:
            print(
                f"coalescing stale {crate}@{requested} event to registry latest "
                f"{registry_latest}"
            )
        resolved[crate] = version
    return resolved


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--crate", action="append", dest="crates", default=[])
    parser.add_argument("--version", default="")
    parser.add_argument("--registry-url", default="https://kinlab.ai")
    parser.add_argument("--manifest", action="append", default=[])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the pins that would change without writing manifests or touching Cargo.lock",
    )
    args = parser.parse_args(argv)

    manifests = args.manifest or ["Cargo.toml"]
    if not args.crates:
        print("no crate supplied; nothing to update")
        return 2

    try:
        versions = _resolve_versions(args.crates, args.version, args.registry_url)
        plans = plan_manifests(manifests, versions)
        changes = apply_plans(plans, versions, dry_run=args.dry_run)
    except (UpdateError, OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"dependency wave aborted: {exc}", file=sys.stderr)
        return 1

    prefix = "[dry-run] would update" if args.dry_run else "updated"
    for manifest, crate, version in changes:
        print(f"{prefix} {manifest}: {crate} -> {version}")
    return 0 if changes else 2


if __name__ == "__main__":
    raise SystemExit(main())
