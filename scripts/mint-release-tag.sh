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

emit_status() {
  printf 'release_tag_status=%s\n' "$1" >> "${GITHUB_OUTPUT:-/dev/null}"
}

remote_tag_commit() {
  local refs code
  set +e
  refs="$(git ls-remote --exit-code --tags origin \
    "refs/tags/${tag}" "refs/tags/${tag}^{}" 2>/dev/null)"
  code=$?
  set -e
  case "$code" in
    0)
      printf '%s\n' "$refs" | awk '
        $2 ~ /\^\{\}$/ { peeled=$1 }
        $2 !~ /\^\{\}$/ { direct=$1 }
        END {
          if (peeled != "") print peeled
          else print direct
        }
      '
      ;;
    2)
      # `git ls-remote --exit-code` uses 2 for a successful query with no
      # matching ref.
      return 1
      ;;
    *)
      echo "::error::could not determine whether ${tag} exists on origin" >&2
      return 2
      ;;
  esac
}

if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+(\.[0-9A-Za-z.]+)*)?$ ]]; then
  echo "::error::refusing to mint: version '${version}' is not a semver release string" >&2
  exit 1
fi

# The remote ref is authoritative. A retry after a successful push must report
# "already present" only when the tag resolves to the exact published commit;
# a same-name tag at another commit is a release-integrity failure.
set +e
remote_existing="$(remote_tag_commit)"
remote_result=$?
set -e
if [[ "$remote_result" == "0" ]]; then
  if [[ "$remote_existing" != "$sha" ]]; then
    echo "::error::${tag} already exists on origin at ${remote_existing}, not the published commit ${sha}" >&2
    exit 1
  fi
  echo "${tag} already exists on origin at ${sha}; release tag is complete"
  emit_status "already-present"
  exit 0
fi
if [[ "$remote_result" != "1" ]]; then
  exit "$remote_result"
fi

# A ref pushed with the default GITHUB_TOKEN does not start further workflow
# runs, by GitHub's recursion guard. Minting with it would create a tag that
# looks released while release.yml never fires, which is worse than not tagging:
# the version is consumed, so a later correct push cannot re-trigger it. Refuse.
# This check intentionally follows the exact remote admission above: a recovery
# rerun can still prove an already-completed tag after credentials are rotated.
if [[ -z "$token" ]]; then
  echo "::error::no release-tag credential. Supply KIN_RELEASE_BOT_APP_ID + KIN_RELEASE_BOT_PRIVATE_KEY (preferred), or KIN_RELEASE_TAG_TOKEN with contents:write." >&2
  echo "::error::refusing to mint ${tag} with the default GITHUB_TOKEN: that tag would not trigger release.yml, and would consume the version." >&2
  exit 1
fi

# A local tag can be left behind by a runner retry even when the remote write
# never completed. Reuse it only if it resolves to the intended commit;
# otherwise replace the local-only ref before pushing.
if git rev-parse -q --verify "refs/tags/${tag}" >/dev/null 2>&1; then
  local_existing="$(git rev-list -n1 "refs/tags/${tag}")"
  if [[ "$local_existing" != "$sha" ]]; then
    git tag -d "$tag" >/dev/null
  fi
fi

if ! git rev-parse -q --verify "refs/tags/${tag}" >/dev/null 2>&1; then
git -c user.name="kin-ci-bot" -c user.email="ci@firelock.io" \
  tag -a "$tag" -m "Release ${version}" "$sha"
fi

repo="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
# Push over an explicit tokenized URL so the credential does not depend on how
# the checkout step was configured. The URL is never echoed.
# A stored credential helper outranks the credential in a push URL, so any
# leftover header would silently authenticate as something other than the token
# resolved above. Drop them for this invocation only.
git config --local --unset-all "http.https://github.com/.extraheader" 2>/dev/null || true

# Report what git actually said. Asserting a cause here once sent the diagnosis
# in the wrong direction: a read-only credential leaking in from the checkout
# was reported as the App lacking contents:write, which it did not.
if ! push_err="$(git push "https://x-access-token:${token}@github.com/${repo}.git" "refs/tags/${tag}" 2>&1)"; then
  # The response can be lost after GitHub accepts the push. Re-read the remote
  # before declaring failure so a safe retry does not misreport a completed tag.
  set +e
  remote_after_push="$(remote_tag_commit)"
  remote_after_result=$?
  set -e
  if [[ "$remote_after_result" == "0" && "$remote_after_push" == "$sha" ]]; then
    echo "${tag} is present on origin at ${sha}; treating the interrupted push as complete"
    emit_status "already-present"
    exit 0
  fi
  # Never echo the URL back, it carries the token.
  echo "::error::failed to push ${tag}. git said:" >&2
  printf '%s\n' "$push_err" | sed "s#https://[^@]*@#https://***@#g" | sed 's/^/    /' >&2
  echo "::error::check that the release App is installed on ${repo} with contents:write, and that no tag ruleset blocks it." >&2
  git tag -d "$tag" >/dev/null 2>&1 || true
  exit 1
fi

echo "minted ${tag} at ${sha}"
echo "release-tag=${tag}" >> "${GITHUB_OUTPUT:-/dev/null}"
emit_status "minted"
