#!/usr/bin/env python3
import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


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


def update_manifest(path, crate, version):
    text = Path(path).read_text(encoding="utf-8")
    changed = False

    def replace_line(match):
        nonlocal changed
        line = match.group(0)
        if 'registry = "kin"' not in line:
            return line
        new = re.sub(r'version\s*=\s*"[^"]+"', f'version = "{version}"', line, count=1)
        if new != line:
            changed = True
        return new

    dep_name = re.escape(crate)
    pattern = re.compile(rf'(?m)^\s*{dep_name}\s*=\s*\{{[^\n]*\}}')
    new_text = pattern.sub(replace_line, text)

    package_pattern = re.compile(rf'(?m)^\s*[\w-]+\s*=\s*\{{[^\n]*package\s*=\s*"{dep_name}"[^\n]*\}}')
    new_text = package_pattern.sub(replace_line, new_text)

    if changed:
        Path(path).write_text(new_text, encoding="utf-8")
    return changed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--crate", action="append", dest="crates", default=[])
    parser.add_argument("--version", default="")
    parser.add_argument("--registry-url", default="https://kinlab.ai")
    parser.add_argument("--manifest", action="append", default=[])
    args = parser.parse_args()

    manifests = args.manifest or ["Cargo.toml"]
    crates = args.crates
    if not crates:
        print("no crate supplied; nothing to update")
        return 0

    changed_any = False
    for crate in crates:
        version = args.version or latest_version(args.registry_url, crate)
        if not version:
            print(f"no published version found for {crate}; skipping")
            continue
        crate_changed = False
        for manifest in manifests:
            path = Path(manifest)
            if path.exists() and update_manifest(path, crate, version):
                crate_changed = True
                changed_any = True
                print(f"updated {manifest}: {crate} -> {version}")
        if crate_changed and Path("Cargo.lock").exists():
            subprocess.run(["cargo", "update", "-p", crate, "--precise", version], check=False)

    return 0 if changed_any else 2


if __name__ == "__main__":
    raise SystemExit(main())
