#!/usr/bin/env bash
set -euo pipefail

package="${PACKAGE:?PACKAGE is required}"
registry_url="${KINLAB_CARGO_REGISTRY_URL:-https://kinlab.ai}"
registry_url="${registry_url%/}"
registry_token="${KINLAB_CARGO_TOKEN:-${KINLAB_TOKEN:-}}"
dry_run="${DRY_RUN:-0}"

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

index_path_for() {
  local name
  name="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "${#name}" in
    1) printf '1/%s' "$name" ;;
    2) printf '2/%s' "$name" ;;
    3) printf '3/%s/%s' "${name:0:1}" "$name" ;;
    *) printf '%s/%s/%s' "${name:0:2}" "${name:2:2}" "$name" ;;
  esac
}

metadata="$tmpdir/metadata.json"
cargo metadata --no-deps --format-version 1 >"$metadata"

version="$(python3 - "$metadata" "$package" <<'PY'
import json, sys
metadata, package = sys.argv[1], sys.argv[2]
for pkg in json.load(open(metadata, encoding="utf-8"))["packages"]:
    if pkg["name"] == package:
        print(pkg["version"])
        raise SystemExit(0)
raise SystemExit(f"package not found: {package}")
PY
)"

tag_name="${TAG_NAME:-}"
if [[ -z "$tag_name" && "${GITHUB_REF_TYPE:-}" == "tag" ]]; then
  tag_name="${GITHUB_REF_NAME:-}"
fi
if [[ -n "$tag_name" ]]; then
  [[ "$tag_name" == v* ]] || { echo "release tag must start with v: $tag_name" >&2; exit 1; }
  expected="${tag_name#v}"
  [[ "$version" == "$expected" ]] || {
    echo "$package version $version does not match tag $tag_name" >&2
    exit 1
  }
fi

index_file="$tmpdir/index"
index_url="${registry_url}/registry/cargo/$(index_path_for "$package")"
if curl -fsSL "$index_url" -o "$index_file" 2>/dev/null; then
  existing_cksum="$(python3 - "$index_file" "$version" <<'PY'
import json, sys
path, version = sys.argv[1], sys.argv[2]
for line in open(path, encoding="utf-8"):
    if not line.strip():
        continue
    obj = json.loads(line)
    if obj.get("vers") == version and not obj.get("yanked", False):
        print(obj.get("cksum", ""))
        break
PY
)"
else
  existing_cksum=""
fi

echo "Packaging $package@$version"
cargo package -p "$package" --allow-dirty --no-verify
crate_file="target/package/${package}-${version}.crate"
[[ -f "$crate_file" ]] || { echo "missing packaged crate: $crate_file" >&2; exit 1; }
local_cksum="$(sha256_file "$crate_file")"

if [[ -n "$existing_cksum" ]]; then
  if [[ "$existing_cksum" == "$local_cksum" ]]; then
    echo "$package@$version already published with matching checksum; no-op."
    exit 0
  fi
  echo "$package@$version is already published with a different checksum." >&2
  echo "  registry: $existing_cksum" >&2
  echo "  local   : $local_cksum" >&2
  exit 1
fi

if [[ "$dry_run" == "1" || "$dry_run" == "true" ]]; then
  echo "[dry-run] Would publish $package@$version"
  exit 0
fi

[[ -n "$registry_token" ]] || {
  echo "KINLAB_CARGO_TOKEN is required to publish $package@$version" >&2
  exit 1
}

response="$tmpdir/response"
code="$(curl -sS -o "$response" -w '%{http_code}' \
  -X POST "${registry_url}/registry/cargo/api/v1/crates/publish?name=${package}&version=${version}" \
  -H "content-type: application/octet-stream" \
  -H "authorization: Bearer ${registry_token}" \
  --data-binary "@${crate_file}")"

case "$code" in
  200|201|204) echo "Published $package@$version" ;;
  409) echo "$package@$version was published by a concurrent job; verifying checksum" ;;
  *) echo "Publish failed for $package@$version (HTTP $code)" >&2; cat "$response" >&2 || true; exit 1 ;;
esac

for _ in 1 2 3 4 5; do
  if curl -fsSL "$index_url" -o "$index_file" 2>/dev/null; then
    published_cksum="$(python3 - "$index_file" "$version" <<'PY'
import json, sys
path, version = sys.argv[1], sys.argv[2]
for line in open(path, encoding="utf-8"):
    if not line.strip():
        continue
    obj = json.loads(line)
    if obj.get("vers") == version and not obj.get("yanked", False):
        print(obj.get("cksum", ""))
        break
PY
)"
    [[ -n "$published_cksum" ]] && break
  fi
  sleep 2
done

[[ "${published_cksum:-}" == "$local_cksum" ]] || {
  echo "published checksum mismatch for $package@$version" >&2
  echo "  registry: ${published_cksum:-<missing>}" >&2
  echo "  local   : $local_cksum" >&2
  exit 1
}

echo "Verified $package@$version checksum $local_cksum"
