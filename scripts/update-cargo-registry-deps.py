#!/usr/bin/env python3
import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


_SIMPLE_VERSION = (
    r"\d+(?:\.\d+){0,2}"
    r"(?:-[0-9A-Za-z.-]+)?"
    r"(?:\+[0-9A-Za-z.-]+)?"
)
_SIMPLE_REQUIREMENT = re.compile(
    rf"^(?P<operator>[=~^]?)(?P<spacing>\s*)(?P<version>{_SIMPLE_VERSION})$"
)


def sparse_index_path(name):
    name = name.lower()
    if len(name) == 1:
        return f"1/{name}"
    if len(name) == 2:
        return f"2/{name}"
    if len(name) == 3:
        return f"3/{name[0]}/{name}"
    return f"{name[:2]}/{name[2:4]}/{name}"


def parse_version(v):
    core = re.split(r"[-+]", v, maxsplit=1)[0]
    parts = []
    for part in core.split(".")[:3]:
        parts.append(int(part) if part.isdigit() else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def latest_version(registry_url, crate):
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


def update_requirement(requirement, version):
    """Move a simple Cargo requirement without changing its operator semantics.

    Bare requirements are Cargo caret requirements, while ``=``, ``~``, and
    ``^`` carry distinct compatibility promises. A dependency wave may move
    the selected version, but it must never silently widen an exact pin or
    reinterpret another operator. Complex ranges fail loud for manual review
    instead of being collapsed to a different requirement.
    """
    if not re.fullmatch(_SIMPLE_VERSION, version):
        raise ValueError(f"unsupported target version: {version!r}")
    match = _SIMPLE_REQUIREMENT.fullmatch(requirement)
    if match is None:
        raise ValueError(
            f"unsupported Cargo requirement {requirement!r}; "
            "dependency-wave updates require a simple bare, =, ~, or ^ version"
        )
    return f"{match.group('operator')}{match.group('spacing')}{version}"


def update_manifest(path, crate, version, dry_run=False):
    text = Path(path).read_text(encoding="utf-8")
    changed = False

    def replace_line(match):
        nonlocal changed
        line = match.group(0)
        if 'registry = "kin"' not in line:
            return line

        def replace_version(version_match):
            requirement = version_match.group("requirement")
            updated = update_requirement(requirement, version)
            return (
                f'{version_match.group("prefix")}"{updated}"'
            )

        new = re.sub(
            r'(?P<prefix>version\s*=\s*)"(?P<requirement>[^"]+)"',
            replace_version,
            line,
            count=1,
        )
        if new != line:
            changed = True
        return new

    dep_name = re.escape(crate)
    pattern = re.compile(rf'(?m)^\s*{dep_name}\s*=\s*\{{[^\n]*\}}')
    new_text = pattern.sub(replace_line, text)

    package_pattern = re.compile(rf'(?m)^\s*[\w-]+\s*=\s*\{{[^\n]*package\s*=\s*"{dep_name}"[^\n]*\}}')
    new_text = package_pattern.sub(replace_line, new_text)

    if changed and not dry_run:
        Path(path).write_text(new_text, encoding="utf-8")
    return changed


def main():
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
    args = parser.parse_args()

    manifests = args.manifest or ["Cargo.toml"]
    crates = args.crates
    if not crates:
        print("no crate supplied; nothing to update")
        return 0

    prefix = "[dry-run] would update" if args.dry_run else "updated"
    changed_any = False
    for crate in crates:
        version = args.version or latest_version(args.registry_url, crate)
        if not version:
            print(f"no published version found for {crate}; skipping")
            continue
        crate_changed = False
        for manifest in manifests:
            path = Path(manifest)
            if path.exists() and update_manifest(path, crate, version, dry_run=args.dry_run):
                crate_changed = True
                changed_any = True
                print(f"{prefix} {manifest}: {crate} -> {version}")
        if crate_changed and not args.dry_run and Path("Cargo.lock").exists():
            subprocess.run(["cargo", "update", "-p", crate, "--precise", version], check=False)

    return 0 if changed_any else 2


if __name__ == "__main__":
    raise SystemExit(main())
