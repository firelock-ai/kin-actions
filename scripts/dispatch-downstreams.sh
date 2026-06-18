#!/usr/bin/env bash
set -euo pipefail

manifest="${DOWNSTREAM_MANIFEST:-.kin-release/downstreams.json}"
token="${KIN_DOWNSTREAM_DISPATCH_TOKEN:-${KIN_CI_BOT_TOKEN:-${GH_TOKEN:-}}}"
package="${PACKAGE:?PACKAGE is required}"
version="${VERSION:?VERSION is required}"

if [[ ! -f "$manifest" ]]; then
  echo "No downstream manifest at $manifest; nothing to dispatch."
  exit 0
fi

if [[ -z "$token" ]]; then
  echo "No downstream dispatch token configured; skipping dispatch."
  exit 0
fi

python3 - "$manifest" "$package" "$version" "${GITHUB_REPOSITORY:-}" "${GITHUB_SHA:-}" "$token" <<'PY'
import json
import sys
import urllib.request

manifest, package, version, source_repo, source_sha, token = sys.argv[1:]
data = json.load(open(manifest, encoding="utf-8"))
rows = data.get("downstreams", data if isinstance(data, list) else [])

for row in rows:
    repo = row["repo"] if isinstance(row, dict) else row
    payload = {
        "event_type": "kin-registry-release",
        "client_payload": {
            "crate_name": package,
            "crate_version": version,
            "source_repo": source_repo,
            "source_sha": source_sha,
        },
    }
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/dispatches",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        if response.status not in (200, 201, 202, 204):
            raise SystemExit(f"dispatch to {repo} failed: HTTP {response.status}")
    print(f"dispatched {package}@{version} to {repo}")
PY
