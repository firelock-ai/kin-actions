#!/usr/bin/env bash
set -euo pipefail

# Resume only the post-publication stages for the exact current Cargo version.
# This script never publishes or moves a tag. It is safe to run after every
# registry workflow completion and on a schedule.

package="${PACKAGE:?PACKAGE is required}"
manifest="${MANIFEST:-Cargo.toml}"
required_workflow="${REQUIRED_WORKFLOW:-CI}"
registry_workflow="${REGISTRY_WORKFLOW:-Registry Publish}"
release_workflow="${RELEASE_WORKFLOW:-Release}"
registry_url="${KINLAB_CARGO_REGISTRY_URL:-https://kinlab.ai}"
downstream_manifest="${DOWNSTREAM_MANIFEST:-.kin-release/downstreams.json}"
helper="${KIN_ACTIONS_HELPER:?KIN_ACTIONS_HELPER is required}"
read_token="${KIN_READ_TOKEN:?KIN_READ_TOKEN is required}"
actions_token="${KIN_ACTIONS_TOKEN:?KIN_ACTIONS_TOKEN is required}"
release_token="${KIN_RELEASE_TAG_TOKEN:?KIN_RELEASE_TAG_TOKEN is required}"
summary="${KIN_RECOVERY_SUMMARY:-${RUNNER_TEMP:-/tmp}/cargo-release-recovery.md}"
smoke_marker="<!-- kin-cargo-release:consumer-smoke-passed -->"
downstream_marker="<!-- kin-cargo-release:downstreams-dispatched -->"
notes_file=""
trap 'rm -f "${notes_file:-}"' EXIT

emit() {
  printf '%s=%s\n' "$1" "$2" >> "${GITHUB_OUTPUT:-/dev/null}"
}

note() {
  printf '%s\n' "$*" | tee -a "$summary"
}

has_marker() {
  grep -Fqx "$1" <<<"$body"
}

append_marker() {
  local value="$1"
  notes_file="$(mktemp)"
  {
    printf '%s' "$body"
    if [[ -n "$body" && "${body: -1}" != $'\n' ]]; then printf '\n'; fi
    printf '\n%s\n' "$value"
  } > "$notes_file"
  gh release edit "$tag" --repo "$GITHUB_REPOSITORY" \
    --notes-file "$notes_file"
  body="$(cat "$notes_file")"
  rm -f "$notes_file"
  notes_file=""
}

: > "$summary"
inspection="$(
  python3 "$helper/scripts/prepare-cargo-release.py" \
    --inspect --manifest "$manifest"
)"
version="$(jq -r .current_version <<<"$inspection")"
tag="v${version}"
emit version "$version"
emit tag "$tag"
note "# Cargo release recovery: ${package}@${version}"

head="$(git rev-parse HEAD)"
live="$(
  GH_TOKEN="$read_token" gh api \
    "repos/${GITHUB_REPOSITORY}/commits/${GITHUB_EVENT_REPOSITORY_DEFAULT_BRANCH}" \
    --jq .sha
)"
if [[ "$head" != "$live" ]]; then
  note "FAIL: checked-out default branch $head moved to $live."
  exit 1
fi

version_commit="$(
  python3 "$helper/scripts/resolve-version-commit.py" \
    --target-version "$version" --cargo-manifest "$manifest"
)"
emit version_commit "$version_commit"
note "- exact version commit: \`${version_commit}\`"

conclusion="$(
  GH_TOKEN="$read_token" gh run list \
    --workflow "$required_workflow" --commit "$version_commit" \
    --event push --limit 20 --json conclusion \
    --jq '.[0].conclusion // ""'
)"
if [[ "$conclusion" != "success" ]]; then
  note "FAIL: exact version commit has no successful ${required_workflow} push run."
  exit 1
fi

registry="$(
  python3 "$helper/scripts/inspect-registry-version.py" \
    --registry-url "$registry_url" \
    --package "$package" --version "$version"
)"
registry_state="$(jq -r .state <<<"$registry")"
emit registry_state "$registry_state"
case "$registry_state" in
  unpublished|version-absent)
    emit recovery_state awaiting-publication
    note "- registry: ${registry_state}; recovering only the exact bounded Registry Publish run."
    KIN_ACTIONS_TOKEN="$actions_token" \
      python3 "$helper/scripts/recover-registry-publish.py" \
        --repository "$GITHUB_REPOSITORY" \
        --package "$package" \
        --version "$version" \
        --version-commit "$version_commit" \
        --workflow "$registry_workflow" \
        --default-branch "$GITHUB_EVENT_REPOSITORY_DEFAULT_BRANCH" \
        --registry-url "$registry_url" \
        --helper-root "$helper"
    registry="$(
      python3 "$helper/scripts/inspect-registry-version.py" \
        --registry-url "$registry_url" \
        --package "$package" --version "$version"
    )"
    registry_state="$(jq -r .state <<<"$registry")"
    emit registry_state "$registry_state"
    if [[ "$registry_state" != "available" ]]; then
      note "FAIL: exact registry row remains ${registry_state} after bounded publication recovery."
      exit 1
    fi
    note "- registry: exact non-yanked row became available after bounded publication recovery."
    ;;
  yanked)
    note "FAIL: exact registry row is yanked; refusing release recovery."
    exit 1
    ;;
  available)
    note "- registry: exact non-yanked row present."
    ;;
  *)
    note "FAIL: unknown registry state ${registry_state}."
    exit 1
    ;;
esac

# Resolve durable tag and Release state before doing work. A same-name tag at
# another commit is terminal: recovery never moves, deletes, or force-updates it.
remote_refs="$(
  git ls-remote --tags origin \
    "refs/tags/${tag}" "refs/tags/${tag}^{}"
)"
tag_sha="$(
  printf '%s\n' "$remote_refs" | awk '
    $2 ~ /\^\{\}$/ { peeled=$1 }
    $2 !~ /\^\{\}$/ { direct=$1 }
    END {
      if (peeled != "") print peeled
      else print direct
    }
  '
)"
tag_present=false
if [[ -n "$tag_sha" ]]; then
  tag_present=true
  if [[ "$tag_sha" != "$version_commit" ]]; then
    note "FAIL: ${tag} exists at ${tag_sha}, not exact version commit ${version_commit}."
    exit 1
  fi
fi

release=""
body=""
if [[ "$tag_present" == "true" ]]; then
  release="$(
    gh release view "$tag" --repo "$GITHUB_REPOSITORY" \
      --json isDraft,isPrerelease,body 2>/dev/null || true
  )"
  if [[ -n "$release" ]]; then
    if [[ "$(jq -r .isDraft <<<"$release")" != "false" ||
          "$(jq -r .isPrerelease <<<"$release")" != "false" ]]; then
      note "FAIL: ${tag} GitHub Release is draft or prerelease."
      exit 1
    fi
    body="$(jq -r '.body // ""' <<<"$release")"
  fi
fi

# The downstream marker can only be written after consumer proof. Otherwise a
# separate smoke marker prevents every scheduled reconciliation from rebuilding
# an already-proven version.
if [[ -n "$release" ]] &&
    { has_marker "$smoke_marker" || has_marker "$downstream_marker"; }; then
  note "- fresh registry-only consumer smoke: durable marker already present."
else
  smoke_seconds="${KIN_RECOVERY_SMOKE_TIMEOUT_SECONDS:-900}"
  timeout "$smoke_seconds" env \
    PACKAGE="$package" VERSION="$version" \
    KINLAB_CARGO_REGISTRY_URL="$registry_url" \
    bash "$helper/scripts/consumer-smoke.sh"
  note "- fresh registry-only consumer smoke: passed."
fi

# mint-release-tag.sh admits an existing tag only at this exact commit and
# never force-updates or deletes a remote ref.
if [[ "$tag_present" == "false" ]]; then
  VERSION="$version" \
  GITHUB_SHA="$version_commit" \
  KIN_RELEASE_TAG_TOKEN="$release_token" \
    bash "$helper/scripts/mint-release-tag.sh"
else
  note "- immutable tag: already present at exact version commit."
fi
note "- immutable tag: \`${tag}\` at exact version commit."

release_run="$(
  TAG="$tag" \
  VERSION_COMMIT="$version_commit" \
  RELEASE_WORKFLOW="$release_workflow" \
  KIN_ACTIONS_TOKEN="$actions_token" \
    bash "$helper/scripts/wait-tag-release-run.sh"
)"
release_run_id="$(jq -r .run_id <<<"$release_run")"
release_run_attempt="$(jq -r .attempt <<<"$release_run")"
note "- tag Release gate: exact ${release_workflow} run ${release_run_id} succeeded at attempt ${release_run_attempt}."

# The tag workflow, not recovery, owns public Release creation. Refresh only
# after its exact run succeeds so a queued or failed validation can never be
# converted into a finalized Release by the reconciler.
release="$(
  gh release view "$tag" --repo "$GITHUB_REPOSITORY" \
    --json isDraft,isPrerelease,body 2>/dev/null || true
)"
if [[ -z "$release" ]]; then
  note "FAIL: exact ${release_workflow} run succeeded but ${tag} has no GitHub Release."
  exit 1
fi
if [[ "$(jq -r .isDraft <<<"$release")" != "false" ||
      "$(jq -r .isPrerelease <<<"$release")" != "false" ]]; then
  note "FAIL: ${tag} GitHub Release is draft or prerelease."
  exit 1
fi
note "- GitHub Release: finalized."

body="$(jq -r '.body // ""' <<<"$release")"
if has_marker "$downstream_marker"; then
  emit recovery_state complete
  note "- all post-publish markers already present; no recovery work needed."
  exit 0
fi
if ! has_marker "$smoke_marker"; then
  append_marker "$smoke_marker"
  note "- fresh consumer proof: durably marked."
fi

if has_marker "$downstream_marker"; then
  note "- downstream delivery: durable marker already present."
else
  PACKAGE="$package" \
  VERSION="$version" \
  GITHUB_SHA="$version_commit" \
  DOWNSTREAM_MANIFEST="$downstream_manifest" \
  KIN_DOWNSTREAM_DISPATCH_TOKEN="${KIN_DOWNSTREAM_DISPATCH_TOKEN:-}" \
    bash "$helper/scripts/dispatch-downstreams.sh"
  append_marker "$downstream_marker"
  note "- downstream delivery: dispatched and durably marked."
fi

emit recovery_state complete
note ""
note "COMPLETE: publication, consumer proof, exact tag, finalized Release, and downstream marker are reconciled."
