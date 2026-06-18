#!/usr/bin/env bash
set -euo pipefail

package="${PACKAGE:?PACKAGE is required}"
version="${VERSION:-latest}"
registry_url="${KINLAB_CARGO_REGISTRY_URL:-https://kinlab.ai}"
registry_url="${registry_url%/}"
registry_index="sparse+${registry_url}/registry/cargo/"

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

if [[ "$version" == "latest" || -z "$version" ]]; then
  req="*"
  status="$(curl -sS -o /dev/null -w '%{http_code}' "${registry_url}/registry/cargo/$(index_path_for "$package")" 2>/dev/null || echo 000)"
  if [[ "$status" == "404" ]]; then
    echo "No published $package yet; skipping latest consumer smoke."
    exit 0
  fi
else
  req="=$version"
fi

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT
consumer="$workdir/consumer"
mkdir -p "$consumer/src" "$consumer/.cargo" "$workdir/cargo-home"
export CARGO_HOME="$workdir/cargo-home"

cat >"$consumer/.cargo/config.toml" <<EOF
[registries.kin]
index = "$registry_index"
EOF

cat >"$consumer/Cargo.toml" <<EOF
[package]
name = "kin-registry-consumer-smoke"
version = "0.0.0"
edition = "2021"
publish = false

[dependencies]
$package = { version = "$req", registry = "kin" }
EOF

cat >"$consumer/src/main.rs" <<'EOF'
fn main() {}
EOF

echo "Building fresh-cache consumer for $package ($req)"
(
  cd "$consumer"
  cargo generate-lockfile
  cargo tree --quiet -p "$package" --depth 0 || true
  cargo build
)
