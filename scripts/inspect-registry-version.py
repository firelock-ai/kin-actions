#!/usr/bin/env python3
"""Inspect one exact immutable Kin Cargo registry row."""

from __future__ import annotations

import argparse
import json
import sys

import kin_registry_index as registry


def inspect(registry_url: str, package: str, version: str) -> dict[str, object]:
    registry.parse_version(version)
    rows = registry.fetch_index(registry_url, package)
    if rows is None:
        return {
            "package": package,
            "version": version,
            "state": "unpublished",
        }
    exact = [row for row in rows if row.version == version]
    if len(exact) > 1:
        raise registry.RegistryIndexError(
            f"duplicate immutable registry rows for {package}@{version}"
        )
    if not exact:
        return {
            "package": package,
            "version": version,
            "state": "version-absent",
        }
    row = exact[0]
    return {
        "package": package,
        "version": version,
        "state": "yanked" if row.yanked else "available",
        "checksum": row.checksum,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry-url", default="https://kinlab.ai")
    parser.add_argument("--package", required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args(argv)
    try:
        result = inspect(args.registry_url, args.package, args.version)
    except (ValueError, registry.RegistryIndexError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
