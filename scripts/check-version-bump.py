#!/usr/bin/env python3
"""Kin registry version-bump gate.

Decide whether a PR/commit MUST carry a crate version bump, using clear,
intent-aware rules so that non-releasing chores stop being blocked:

  * Release-affecting changes REQUIRE a version bump:
      - crate/package source trees: ``src/**``, ``crates/**/src/**``,
        ``packages/**/src/**`` and build scripts (``build.rs``)
      - ``Cargo.toml`` dependency / feature changes (the parts of a manifest
        that change what downstream consumers actually build)
  * Non-release changes do NOT require a bump (the gate is skipped):
      - tests / benches / examples, documentation and ``*.md`` files,
        comments, CI config (``.github/**``) and other non-source chores
  * A ``release`` / ``release:*`` PR label (or any changed crate ``src/`` path)
    FORCES the bump requirement.

It also guards two registry invariants regardless of the change set:

  * a crate version may never move *below* the newest already-published
    version, and
  * a release-affecting change may not land on a version that is already
    published (that would either be a silent no-op or a corruption risk).

The decision logic lives in :func:`evaluate_gate` and the file classifier in
:func:`classify_path`; both are pure so they can be unit-tested without git,
cargo, or network access (see ``scripts/test_check_version_bump.py``).
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


# --- helpers -------------------------------------------------------------

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


def read_manifest_version_text(text):
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
    raise SystemExit("could not read version from manifest")


def read_manifest_version(path):
    return read_manifest_version_text(Path(path).read_text(encoding="utf-8"))


def git_show_file(ref, path):
    """Return the text of ``path`` at ``ref`` or ``None`` if it does not exist."""
    result = run(["git", "show", f"{ref}:{path}"], check=False)
    if result.returncode != 0:
        return None
    return result.stdout


def base_manifest_version(base_ref, manifest):
    text = git_show_file(base_ref, manifest)
    if text is None:
        return None
    try:
        return read_manifest_version_text(text)
    except SystemExit:
        return None


def changed_files(base_ref):
    result = run(["git", "diff", "--name-only", f"{base_ref}...HEAD"], check=False)
    if result.returncode != 0:
        result = run(["git", "diff", "--name-only", f"{base_ref}", "HEAD"], check=False)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def published_versions(registry_url, crate_name):
    import urllib.error
    import urllib.request

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


# --- file classification -------------------------------------------------

# Path segments that mark non-release locations (tests, fixtures, docs) when
# the file is not inside a `src/` tree.
EXCLUDED_DIR_SEGMENTS = {
    "tests", "test", "benches", "bench", "examples", "example", "fuzz",
    "docs", "doc",
}
# Documentation / prose suffixes that never require a release bump.
DOC_SUFFIXES = (".md", ".mdx", ".markdown", ".rst", ".adoc", ".txt")


def _under_any(path, prefixes):
    for prefix in prefixes:
        p = prefix.strip().strip("/")
        if not p:
            continue
        if path == p or path.startswith(p + "/"):
            return True
    return False


def classify_path(path, extra_source_roots=None):
    """Classify a changed path as ``source``, ``manifest`` or ``ignore``.

    ``source``   -> release-affecting crate/package source code.
    ``manifest`` -> a ``Cargo.toml`` (dep/feature relevance checked separately).
    ``ignore``   -> docs, tests, CI and other non-releasing chores.
    """
    norm = (path or "").strip().replace("\\", "/")
    if not norm:
        return "ignore"
    segments = norm.split("/")
    base = segments[-1]

    if base == "Cargo.toml":
        return "manifest"
    # Documentation / prose, regardless of where it lives.
    if base.lower().endswith(DOC_SUFFIXES):
        return "ignore"
    # CI / tooling config is never release source.
    if segments[0] in {".github", ".cargo"}:
        return "ignore"
    # Anything inside a `src/` tree is genuine crate source -- this also covers
    # a crate that happens to be named `test`/`docs` (e.g. crates/test/src/..).
    if "src" in segments:
        return "source"
    # Build scripts live at the crate root, outside `src/`.
    if base == "build.rs":
        return "source"
    # No `src/` tree: conventional test/bench/example/doc locations don't ship.
    if any(seg in EXCLUDED_DIR_SEGMENTS for seg in segments):
        return "ignore"
    # Non-standard source layouts can be declared via --source-path.
    if extra_source_roots and _under_any(norm, extra_source_roots):
        return "source"
    return "ignore"


# --- manifest dependency / feature change detection ----------------------

def is_release_manifest_table(header):
    """True if a manifest table affects what consumers build.

    Dependency tables (incl. target-specific and per-dependency subtables) and
    the ``[features]`` table are release-relevant. ``[dev-dependencies]`` are
    not (they are stripped from the published artifact), nor is ``[package]``
    metadata such as ``description``/``version``.
    """
    h = header.strip()
    if not h:
        return False
    if "dev-dependencies" in h:
        return False
    if re.search(r"(^|\.)dependencies(\.|$)", h):
        return True
    if re.search(r"(^|\.)build-dependencies(\.|$)", h):
        return True
    if re.search(r"(^|\.)features(\.|$)", h):
        return True
    return False


def manifest_release_signature(text):
    """Normalized slice of a manifest holding only release-relevant lines."""
    lines = []
    active = False
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("[") and s.endswith("]"):
            header = s.strip("[]").strip()
            active = is_release_manifest_table(header)
            if active:
                lines.append(f"[{header}]")
            continue
        if active:
            lines.append(s)
    return "\n".join(lines)


def manifest_deps_changed(base_text, head_text):
    """True if dependency/feature content of a manifest changed.

    A brand-new manifest (``base_text is None``) is treated as release-relevant.
    A pure version bump or metadata edit returns ``False``.
    """
    if base_text is None:
        return True
    return manifest_release_signature(base_text) != manifest_release_signature(head_text or "")


# --- PR labels -----------------------------------------------------------

def parse_labels(raw):
    if not raw:
        return []
    return [t for t in re.split(r"[,\s]+", raw.strip()) if t]


def has_release_label(labels):
    for label in labels:
        low = label.strip().lower()
        if low == "release" or low.startswith("release:") or low.startswith("release/"):
            return True
    return False


# --- decision ------------------------------------------------------------

def evaluate_gate(*, package, version, base_version, published,
                  source_changes, dep_manifest_changes, release_label):
    """Pure gate decision. Returns ``(failures, require_bump, relevant)``."""
    failures = []
    newest = max(published, key=parse_version) if published else None
    if newest and parse_version(version) < parse_version(newest):
        failures.append(f"{package}@{version} is lower than newest published {newest}")

    relevant = list(source_changes) + list(dep_manifest_changes)
    require_bump = bool(relevant) or release_label

    if require_bump and base_version is not None:
        if version == base_version:
            reason = "release label set" if (release_label and not relevant) else \
                f"{len(relevant)} release-affecting file(s) changed"
            failures.append(
                f"{reason} but {package} version stayed at {version}; "
                "bump major/minor/patch before merging"
            )
        elif version in published:
            failures.append(
                f"{package}@{version} is already published; bump again before "
                "merging release-affecting changes"
            )
    return failures, require_bump, relevant


def main():
    parser = argparse.ArgumentParser(description="Kin registry version-bump gate")
    parser.add_argument("--package", required=True)
    parser.add_argument("--manifest", default="Cargo.toml")
    parser.add_argument("--base-ref", default=os.environ.get("BASE_REF", ""))
    parser.add_argument("--registry-url", default=os.environ.get("KINLAB_CARGO_REGISTRY_URL", "https://kinlab.ai"))
    parser.add_argument(
        "--source-path", action="append", default=[],
        help="extra release-source path prefix for non-standard layouts "
             "(standard src/, crates/**/src/, packages/**/src/, build.rs and "
             "Cargo.toml deps are always detected)",
    )
    parser.add_argument(
        "--labels", default=os.environ.get("PR_LABELS", ""),
        help="comma/space separated PR labels; a 'release'/'release:*' label "
             "forces the bump requirement",
    )
    args = parser.parse_args()

    extra_source_roots = args.source_path or []
    version = cargo_metadata_version(args.package)
    try:
        manifest_version = read_manifest_version(args.manifest)
        if version != manifest_version:
            print(f"warning: cargo metadata resolved {args.package}@{version}, "
                  f"manifest has {manifest_version}")
    except (SystemExit, OSError):
        pass

    published = published_versions(args.registry_url, args.package)

    base_ref = args.base_ref
    if not base_ref:
        result = run(["git", "rev-parse", "--verify", "HEAD^"], check=False)
        base_ref = "HEAD^" if result.returncode == 0 else ""

    changed = changed_files(base_ref) if base_ref else []
    base_version = base_manifest_version(base_ref, args.manifest) if base_ref else None

    source_changes = []
    manifest_changes = []
    for path in changed:
        kind = classify_path(path, extra_source_roots)
        if kind == "source":
            source_changes.append(path)
        elif kind == "manifest":
            manifest_changes.append(path)

    dep_manifest_changes = []
    for manifest in manifest_changes:
        base_text = git_show_file(base_ref, manifest) if base_ref else None
        try:
            head_text = Path(manifest).read_text(encoding="utf-8")
        except OSError:
            head_text = ""
        if manifest_deps_changed(base_text, head_text):
            dep_manifest_changes.append(manifest)

    labels = parse_labels(args.labels)
    release_label = has_release_label(labels)

    failures, require_bump, relevant = evaluate_gate(
        package=args.package,
        version=version,
        base_version=base_version,
        published=published,
        source_changes=source_changes,
        dep_manifest_changes=dep_manifest_changes,
        release_label=release_label,
    )

    newest = max(published, key=parse_version) if published else None
    print("Kin version gate")
    print(f"  package           : {args.package}")
    print(f"  version           : {version}")
    print(f"  newest published  : {newest or '<none>'}")
    print(f"  base ref          : {base_ref or '<none>'}")
    print(f"  base version      : {base_version or '<unknown>'}")
    print(f"  release label     : {'yes' if release_label else 'no'}")
    print(f"  source changes    : {len(source_changes)}")
    print(f"  dep manifest chgs : {len(dep_manifest_changes)}")
    print(f"  bump required     : {'yes' if require_bump else 'no'}")
    for path in relevant[:20]:
        print(f"    - {path}")
    if len(relevant) > 20:
        print(f"    ... {len(relevant) - 20} more")

    if failures:
        print("FAIL:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    if not require_bump:
        print("OK: no release-affecting changes; version bump not required.")
    else:
        print("OK: version is acceptable for the changed files and registry state.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
