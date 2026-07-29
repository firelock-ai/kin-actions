#!/usr/bin/env python3
"""Strict Kin sparse-registry index and SemVer authority.

Only an HTTP 404 represents an unpublished crate. Every successful response
must contain at least one valid Cargo index row; transport, decoding, JSON, and
schema failures are authority failures rather than an empty registry.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable


# SemVer numeric identifiers are ASCII by grammar. Python's generic digit
# character class is broader, so every numeric branch stays explicit.
_CORE_IDENTIFIER = r"(?:0|[1-9][0-9]*)"
_PRERELEASE_IDENTIFIER = (
    r"(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
)
_BUILD_IDENTIFIER = r"[0-9A-Za-z-]+"
_SEMVER = re.compile(
    rf"^(?P<major>{_CORE_IDENTIFIER})\."
    rf"(?P<minor>{_CORE_IDENTIFIER})\."
    rf"(?P<patch>{_CORE_IDENTIFIER})"
    rf"(?:-(?P<prerelease>{_PRERELEASE_IDENTIFIER}"
    rf"(?:\.{_PRERELEASE_IDENTIFIER})*))?"
    rf"(?:\+(?P<build>{_BUILD_IDENTIFIER}"
    rf"(?:\.{_BUILD_IDENTIFIER})*))?$"
)
_CHECKSUM = re.compile(r"^[0-9a-fA-F]{64}$")


class RegistryIndexError(RuntimeError):
    """Sparse-index authority could not be established."""


@dataclass(frozen=True)
class RegistryVersion:
    """One immutable version record from a Cargo sparse index."""

    name: str
    version: str
    yanked: bool
    checksum: str


def parse_version(version: str) -> tuple[object, ...]:
    """Return a full SemVer precedence key, ignoring build metadata."""

    match = _SEMVER.fullmatch(version)
    if match is None:
        raise ValueError(f"invalid SemVer: {version!r}")
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


def sparse_index_path(name: str) -> str:
    """Return the Cargo sparse-index path for one crate name."""

    name = name.lower()
    if len(name) == 1:
        return f"1/{name}"
    if len(name) == 2:
        return f"2/{name}"
    if len(name) == 3:
        return f"3/{name[0]}/{name}"
    return f"{name[:2]}/{name[2:4]}/{name}"


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value!r}")


def parse_index(
    body: bytes,
    *,
    crate_name: str,
    source: str,
) -> tuple[RegistryVersion, ...]:
    """Parse and validate every row in a successful sparse-index response."""

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RegistryIndexError(
            f"could not decode registry index {source} as UTF-8: {exc}"
        ) from exc

    records: list[RegistryVersion] = []
    version_rows: dict[str, int] = {}
    rows_seen = 0
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        rows_seen += 1
        try:
            obj = json.loads(
                line,
                parse_constant=_reject_nonstandard_json_constant,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise RegistryIndexError(
                f"malformed registry index row {line_number} at {source}: "
                f"invalid JSON ({exc})"
            ) from exc
        if not isinstance(obj, dict):
            raise RegistryIndexError(
                f"malformed registry index row {line_number} at {source}: "
                "expected an object"
            )

        name = obj.get("name")
        version = obj.get("vers")
        yanked = obj.get("yanked")
        checksum = obj.get("cksum")
        dependencies = obj.get("deps")
        features = obj.get("features")

        if not isinstance(name, str) or not name:
            raise RegistryIndexError(
                f"malformed registry index row {line_number} at {source}: "
                "missing string 'name'"
            )
        if name != crate_name:
            raise RegistryIndexError(
                f"malformed registry index row {line_number} at {source}: "
                f"crate name {name!r} does not match {crate_name!r}"
            )
        if not isinstance(version, str) or not version:
            raise RegistryIndexError(
                f"malformed registry index row {line_number} at {source}: "
                "missing string 'vers'"
            )
        try:
            parse_version(version)
        except ValueError as exc:
            raise RegistryIndexError(
                f"malformed registry index row {line_number} at {source}: {exc}"
            ) from exc
        if version in version_rows:
            raise RegistryIndexError(
                f"malformed registry index row {line_number} at {source}: "
                f"duplicate immutable version {crate_name}@{version}; "
                f"first seen on row {version_rows[version]}"
            )
        version_rows[version] = line_number
        if not isinstance(yanked, bool):
            raise RegistryIndexError(
                f"malformed registry index row {line_number} at {source}: "
                "missing boolean 'yanked'"
            )
        if not isinstance(checksum, str) or not _CHECKSUM.fullmatch(checksum):
            raise RegistryIndexError(
                f"malformed registry index row {line_number} at {source}: "
                "missing 64-hex 'cksum'"
            )
        if not isinstance(dependencies, list):
            raise RegistryIndexError(
                f"malformed registry index row {line_number} at {source}: "
                "missing list 'deps'"
            )
        if not isinstance(features, dict):
            raise RegistryIndexError(
                f"malformed registry index row {line_number} at {source}: "
                "missing object 'features'"
            )

        records.append(
            RegistryVersion(
                name=name,
                version=version,
                yanked=yanked,
                checksum=checksum.lower(),
            )
        )

    if rows_seen == 0:
        raise RegistryIndexError(
            f"malformed registry index at {source}: empty successful response; "
            "an unpublished crate must return HTTP 404"
        )
    return tuple(records)


def fetch_index(
    registry_url: str,
    crate_name: str,
    *,
    timeout: float = 10,
    opener: Callable[..., object] | None = None,
) -> tuple[RegistryVersion, ...] | None:
    """Fetch one strict sparse index, returning ``None`` only for HTTP 404."""

    if opener is None:
        opener = urllib.request.urlopen
    url = (
        f"{registry_url.rstrip('/')}/registry/cargo/"
        f"{sparse_index_path(crate_name)}"
    )
    try:
        with opener(url, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise RegistryIndexError(
            f"could not read registry index {url}: HTTP {exc.code}"
        ) from exc
    except Exception as exc:
        raise RegistryIndexError(
            f"could not read registry index {url}: {exc}"
        ) from exc
    if not isinstance(body, bytes):
        raise RegistryIndexError(
            f"could not read registry index {url}: response body was not bytes"
        )
    return parse_index(body, crate_name=crate_name, source=url)
