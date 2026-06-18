#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


def run(args, *, text=True, check=True):
    return subprocess.run(args, text=text, check=check, capture_output=True)


def parse_version(v):
    core = re.split(r"[-+]", v, maxsplit=1)[0]
    parts = []
    for part in core.split(".")[:3]:
        parts.append(int(part) if part.isdigit() else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def sparse_index_path(name):
    name = name.lower()
    if len(name) == 1:
        return f"1/{name}"
    if len(name) == 2:
        return f"2/{name}"
    if len(name) == 3:
        return f"3/{name[0]}/{name}"
    return f"{name[:2]}/{name[2:4]}/{name}"


def cargo_metadata_version(package):
    data = json.loads(run(["cargo", "metadata", "--no-deps", "--format-version", "1"]).stdout)
    for pkg in data["packages"]:
        if pkg["name"] == package:
            return pkg["version"]
    raise SystemExit(f"package not found in cargo metadata: {package}")


def read_manifest_version(path):
    text = Path(path).read_text(encoding="utf-8")
    in_package = False
    in_workspace_package = False
    first_version = None
    for raw in text.splitlines():
        line = raw.strip()
        if line == "[package]":
            in_package = True
            in_workspace_package = False
            continue
        if line == "[workspace.package]":
            in_package = False
            in_workspace_package = True
            continue
        if line.startswith("[") and line.endswith("]"):
            in_package = False
            in_workspace_package = False
        match = re.match(r'version\s*=\s*"([^"]+)"', line)
        if match:
            first_version = first_version or match.group(1)
            if in_package or in_workspace_package:
                return match.group(1)
    if first_version:
        return first_version
    raise SystemExit(f"could not read version from {path}")


def base_manifest_version(base_ref, manifest):
    result = run(["git", "show", f"{base_ref}:{manifest}"], check=False)
    if result.returncode != 0:
        return None
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as fh:
        fh.write(result.stdout)
        temp = fh.name
    try:
        return read_manifest_version(temp)
    finally:
        Path(temp).unlink(missing_ok=True)


def changed_files(base_ref):
    result = run(["git", "diff", "--name-only", f"{base_ref}...HEAD"], check=False)
    if result.returncode != 0:
        result = run(["git", "diff", "--name-only", f"{base_ref}", "HEAD"], check=False)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def path_matches(path, prefixes):
    return any(path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in prefixes)


def published_versions(registry_url, crate_name):
    url = f"{registry_url.rstrip('/')}/registry/cargo/{sparse_index_path(crate_name)}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        print(f"warning: could not read registry index {url}: HTTP {exc.code}", file=sys.stderr)
        return []
    except OSError as exc:
        print(f"warning: could not read registry index {url}: {exc}", file=sys.stderr)
        return []
    versions = []
    for line in body.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not obj.get("yanked", False):
            versions.append(obj.get("vers", ""))
    return [v for v in versions if v]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", required=True)
    parser.add_argument("--manifest", default="Cargo.toml")
    parser.add_argument("--base-ref", default=os.environ.get("BASE_REF", ""))
    parser.add_argument("--registry-url", default=os.environ.get("KINLAB_CARGO_REGISTRY_URL", "https://kinlab.ai"))
    parser.add_argument("--source-path", action="append", default=[])
    args = parser.parse_args()

    source_paths = args.source_path or ["src", "crates", "packages", "Cargo.toml"]
    version = cargo_metadata_version(args.package)
    manifest_version = read_manifest_version(args.manifest)
    if version != manifest_version:
        print(f"warning: cargo metadata resolved {args.package}@{version}, manifest has {manifest_version}")

    published = published_versions(args.registry_url, args.package)
    newest = max(published, key=parse_version) if published else None
    failures = []
    if newest and parse_version(version) < parse_version(newest):
        failures.append(f"{args.package}@{version} is lower than newest published {newest}")

    base_ref = args.base_ref
    if not base_ref:
        result = run(["git", "rev-parse", "--verify", "HEAD^"], check=False)
        base_ref = "HEAD^" if result.returncode == 0 else ""

    changed = []
    base_version = None
    if base_ref:
        changed = changed_files(base_ref)
        base_version = base_manifest_version(base_ref, args.manifest)

    relevant = [p for p in changed if path_matches(p, source_paths)]
    if relevant and base_version:
        if version == base_version:
            failures.append(
                f"{len(relevant)} release-relevant file(s) changed but {args.package} version stayed at {version}"
            )
        elif version in published:
            failures.append(
                f"{args.package}@{version} is already published; bump again before merging source changes"
            )

    print("Kin version gate")
    print(f"  package          : {args.package}")
    print(f"  version          : {version}")
    print(f"  newest published : {newest or '<none>'}")
    print(f"  base ref         : {base_ref or '<none>'}")
    print(f"  base version     : {base_version or '<unknown>'}")
    print(f"  relevant changes : {len(relevant)}")
    for path in relevant[:20]:
        print(f"    - {path}")
    if len(relevant) > 20:
        print(f"    ... {len(relevant) - 20} more")

    if failures:
        print("FAIL:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("OK: version is acceptable for the changed files and registry state.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
