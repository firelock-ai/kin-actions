#!/usr/bin/env bash
set -euo pipefail

manifest="${DOWNSTREAM_MANIFEST:-.kin-release/downstreams.json}"
package="${PACKAGE:?PACKAGE is required}"
version="${VERSION:?VERSION is required}"

if [[ ! -f "$manifest" ]]; then
  echo "No downstream manifest at $manifest; nothing to dispatch."
  exit 0
fi

if [[ -z "${KIN_DOWNSTREAM_DISPATCH_TOKEN:-${KIN_CI_BOT_TOKEN:-${GH_TOKEN:-}}}" ]]; then
  echo "No downstream dispatch token configured; skipping dispatch."
  exit 0
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Keep the token out of argv/process listings. The Python helper resolves the
# existing secret fallback chain from the environment.
export KIN_DOWNSTREAM_DISPATCH_TOKEN="${KIN_DOWNSTREAM_DISPATCH_TOKEN:-${KIN_CI_BOT_TOKEN:-${GH_TOKEN:-}}}"
python3 "$script_dir/dispatch-downstreams.py" \
  "$manifest" \
  "$package" \
  "$version" \
  "${GITHUB_REPOSITORY:-}" \
  "${GITHUB_SHA:-}"
