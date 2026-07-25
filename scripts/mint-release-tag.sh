#!/usr/bin/env bash
set -euo pipefail

# Mint the `v<version>` tag for a version that was just published to the registry.
#
# Registry publication is automated on push-to-main, but each repo's release.yml
# fires only on a `v*.*.*` tag push, and nothing minted those tags. This closes
# that gap so a version bump that reaches main produces its GitHub Release
# without a human remembering to tag, per repo.
#
# Required: VERSION, GITHUB_SHA, GITHUB_REPOSITORY.
#
# The caller supplies KIN_RELEASE_TAG_TOKEN, preferring a short-lived App
# installation token. A dispatch-scoped token is deliberately not accepted:
# it carries repository_dispatch only, so the push fails late and reads as a
# broken release rather than a missing credential.

version="${VERSION:?VERSION is required}"
sha="${GITHUB_SHA:?GITHUB_SHA is required}"
tag="v${version}"

token="${KIN_RELEASE_TAG_TOKEN:-${KIN_CI_BOT_TOKEN:-}}"

# A ref pushed with the default GITHUB_TOKEN does not start further workflow
# runs, by GitHub's recursion guard. Minting with it would create a tag that
# looks released while release.yml never fires, which is worse than not tagging:
# the version is consumed, so a later correct push cannot re-trigger it. Refuse.
if [[ -z "$token" ]]; then
  echo "::error::no release-tag credential. Supply KIN_RELEASE_BOT_APP_ID + KIN_RELEASE_BOT_PRIVATE_KEY (preferred), or KIN_RELEASE_TAG_TOKEN with contents:write." >&2
  echo "::error::refusing to mint ${tag} with the default GITHUB_TOKEN: that tag would not trigger release.yml, and would consume the version." >&2
  exit 1
fi

if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+(\.[0-9A-Za-z.]+)*)?$ ]]; then
  echo "::error::refusing to mint: version '${version}' is not a semver release string" >&2
  exit 1
fi

git fetch --tags --quiet origin || true

# Idempotent: a re-run, or a tag a human already pushed, must not be disturbed.
if git rev-parse -q --verify "refs/tags/${tag}" >/dev/null 2>&1; then
  existing="$(git rev-list -n1 "refs/tags/${tag}")"
  if [[ "$existing" == "$sha" ]]; then
    echo "${tag} already exists at ${sha}; nothing to mint"
  else
    echo "::warning::${tag} already exists at ${existing}, not the published commit ${sha}; leaving it alone"
  fi
  exit 0
fi

if git ls-remote --exit-code --tags origin "refs/tags/${tag}" >/dev/null 2>&1; then
  echo "${tag} already present on origin; nothing to mint"
  exit 0
fi

git -c user.name="kin-ci-bot" -c user.email="ci@firelock.io" \
  tag -a "$tag" -m "Release ${version}" "$sha"

repo="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
# Push over an explicit tokenized URL so the credential does not depend on how
# the checkout step was configured. The URL is never echoed.
if ! git push "https://x-access-token:${token}@github.com/${repo}.git" "refs/tags/${tag}" >/dev/null 2>&1; then
  echo "::error::failed to push ${tag}: the release-tag credential lacks contents:write on ${repo}." >&2
  echo "::error::install the release App on ${repo} and supply KIN_RELEASE_BOT_APP_ID + KIN_RELEASE_BOT_PRIVATE_KEY, or grant contents:write to KIN_RELEASE_TAG_TOKEN." >&2
  git tag -d "$tag" >/dev/null 2>&1 || true
  exit 1
fi

echo "minted ${tag} at ${sha}"
echo "release-tag=${tag}" >> "${GITHUB_OUTPUT:-/dev/null}"
