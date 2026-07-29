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
controller_deadline="${KIN_RECOVERY_DEADLINE_EPOCH:?KIN_RECOVERY_DEADLINE_EPOCH is required}"
authority_timeout="${KIN_RECOVERY_AUTHORITY_TIMEOUT_SECONDS:-120}"
api_timeout="${KIN_RECOVERY_API_TIMEOUT_SECONDS:-120}"
publication_timeout="${KIN_PUBLICATION_RECOVERY_TIMEOUT_SECONDS:-900}"
smoke_timeout="${KIN_RECOVERY_SMOKE_TIMEOUT_SECONDS:-900}"
tag_timeout="${KIN_RECOVERY_TAG_TIMEOUT_SECONDS:-120}"
release_timeout="${KIN_RELEASE_RUN_TIMEOUT_SECONDS:-900}"
downstream_timeout="${KIN_RECOVERY_DOWNSTREAM_TIMEOUT_SECONDS:-300}"
smoke_marker="<!-- kin-cargo-release:consumer-smoke-passed -->"
downstream_marker="<!-- kin-cargo-release:downstreams-dispatched -->"
notes_file=""
current_phase="initialize"
last_state=""
last_state_rank=0

emit() {
  printf '%s=%s\n' "$1" "$2" >> "${GITHUB_OUTPUT:-/dev/null}"
}

note() {
  printf '%s\n' "$*" | tee -a "$summary"
}

state_rank() {
  case "$1" in
    inspecting) printf '10\n' ;;
    awaiting-publication) printf '20\n' ;;
    registry-available) printf '30\n' ;;
    consumer-proven) printf '40\n' ;;
    tag-present) printf '50\n' ;;
    release-finalized) printf '60\n' ;;
    downstreams-dispatched) printf '70\n' ;;
    complete) printf '80\n' ;;
    *)
      printf 'unknown recovery state %s\n' "$1" >&2
      return 1
      ;;
  esac
}

emit_state() {
  local state="$1"
  local rank
  rank="$(state_rank "$state")"
  if ((rank < last_state_rank)); then
    printf 'recovery state regression refused: %s -> %s\n' \
      "${last_state:-unset}" "$state" >&2
    return 1
  fi
  last_state="$state"
  last_state_rank="$rank"
  emit recovery_state "$state"
}

budget_for() {
  local requested="$1"
  local now remaining
  if [[ ! "$requested" =~ ^[1-9][0-9]*$ ]]; then
    printf 'phase timeout must be a positive integer, got %s\n' "$requested" >&2
    return 1
  fi
  now="$(date +%s)"
  remaining=$((controller_deadline - now))
  if ((remaining <= 0)); then
    printf 'aggregate recovery deadline %s is exhausted in phase %s\n' \
      "$controller_deadline" "$current_phase" >&2
    return 1
  fi
  if ((requested < remaining)); then
    printf '%s\n' "$requested"
  else
    printf '%s\n' "$remaining"
  fi
}

hard_timeout() {
  local seconds="$1"
  shift
  timeout --signal=TERM --kill-after=5s "$seconds" "$@"
}

run_bounded() {
  local requested="$1"
  shift
  local seconds
  if ! seconds="$(budget_for "$requested")"; then
    return 1
  fi
  hard_timeout "$seconds" "$@"
}

finish() {
  local status=$?
  trap - EXIT
  rm -f "${notes_file:-}"
  if ((status != 0)); then
    emit failed_phase "$current_phase" || true
    {
      # shellcheck disable=SC2016 # Backticks are Markdown, not shell syntax.
      printf '\nFAIL: controller stopped in phase `%s`; last proven recovery state is `%s`.\n' \
        "$current_phase" "${last_state:-unresolved}"
    } >> "$summary" 2>/dev/null || true
  fi
  exit "$status"
}
trap finish EXIT

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
  run_bounded "$api_timeout" gh release edit "$tag" --repo "$GITHUB_REPOSITORY" \
    --notes-file "$notes_file"
  body="$(cat "$notes_file")"
  rm -f "$notes_file"
  notes_file=""
}

: > "$summary"
if [[ ! "$controller_deadline" =~ ^[1-9][0-9]*$ ]]; then
  note "FAIL: aggregate recovery deadline must be a positive epoch second."
  exit 1
fi
emit_state inspecting
note "- aggregate controller deadline: \`${controller_deadline}\` (every phase is capped by remaining time)."
current_phase="inspect-authority"
inspection="$(
  run_bounded "$authority_timeout" \
    python3 "$helper/scripts/prepare-cargo-release.py" \
    --inspect --manifest "$manifest"
)"
version="$(jq -r .current_version <<<"$inspection")"
tag="v${version}"
emit version "$version"
emit tag "$tag"
note "# Cargo release recovery: ${package}@${version}"

head="$(run_bounded "$authority_timeout" git rev-parse HEAD)"
live="$(
  run_bounded "$api_timeout" env GH_TOKEN="$read_token" \
    gh api \
      "repos/${GITHUB_REPOSITORY}/commits/${GITHUB_EVENT_REPOSITORY_DEFAULT_BRANCH}" \
      --jq .sha
)"
if [[ "$head" != "$live" ]]; then
  note "FAIL: checked-out default branch $head moved to $live."
  exit 1
fi

version_commit="$(
  run_bounded "$authority_timeout" \
    python3 "$helper/scripts/resolve-version-commit.py" \
      --target-version "$version" --cargo-manifest "$manifest"
)"
emit version_commit "$version_commit"
note "- exact version commit: \`${version_commit}\`"

conclusion="$(
  run_bounded "$api_timeout" env GH_TOKEN="$read_token" \
    gh run list \
      --workflow "$required_workflow" --commit "$version_commit" \
      --event push --limit 20 --json conclusion \
      --jq '.[0].conclusion // ""'
)"
if [[ "$conclusion" != "success" ]]; then
  note "FAIL: exact version commit has no successful ${required_workflow} push run."
  exit 1
fi

registry="$(
  run_bounded "$api_timeout" \
    python3 "$helper/scripts/inspect-registry-version.py" \
      --registry-url "$registry_url" \
      --package "$package" --version "$version"
)"
registry_state="$(jq -r .state <<<"$registry")"
emit registry_state "$registry_state"
case "$registry_state" in
  unpublished|version-absent)
    emit_state awaiting-publication
    current_phase="recover-publication"
    note "- registry: ${registry_state}; recovering only the exact bounded Registry Publish run."
    if ! publication_seconds="$(budget_for "$publication_timeout")"; then
      exit 1
    fi
    hard_timeout "$publication_seconds" env \
      KIN_ACTIONS_TOKEN="$actions_token" \
      python3 "$helper/scripts/recover-registry-publish.py" \
        --repository "$GITHUB_REPOSITORY" \
        --package "$package" \
        --version "$version" \
        --version-commit "$version_commit" \
        --workflow "$registry_workflow" \
        --default-branch "$GITHUB_EVENT_REPOSITORY_DEFAULT_BRANCH" \
        --registry-url "$registry_url" \
        --helper-root "$helper" \
        --timeout-seconds "$publication_seconds"
    registry="$(
      run_bounded "$api_timeout" \
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
    emit_state registry-available
    ;;
  yanked)
    note "FAIL: exact registry row is yanked; refusing release recovery."
    exit 1
    ;;
  available)
    note "- registry: exact non-yanked row present."
    emit_state registry-available
    ;;
  *)
    note "FAIL: unknown registry state ${registry_state}."
    exit 1
    ;;
esac

# Resolve durable tag and Release state before doing work. A same-name tag at
# another commit is terminal: recovery never moves, deletes, or force-updates it.
current_phase="inspect-tag-and-release"
remote_refs="$(
  run_bounded "$api_timeout" \
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
    run_bounded "$api_timeout" \
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
  current_phase="consumer-smoke"
  if ! smoke_seconds="$(budget_for "$smoke_timeout")"; then
    exit 1
  fi
  hard_timeout "$smoke_seconds" env \
    PACKAGE="$package" VERSION="$version" \
    KINLAB_CARGO_REGISTRY_URL="$registry_url" \
    bash "$helper/scripts/consumer-smoke.sh"
  note "- fresh registry-only consumer smoke: passed."
fi
emit_state consumer-proven

# mint-release-tag.sh admits an existing tag only at this exact commit and
# never force-updates or deletes a remote ref.
current_phase="mint-tag"
if [[ "$tag_present" == "false" ]]; then
  if ! tag_seconds="$(budget_for "$tag_timeout")"; then
    exit 1
  fi
  hard_timeout "$tag_seconds" env \
    VERSION="$version" \
    GITHUB_SHA="$version_commit" \
    KIN_RELEASE_TAG_TOKEN="$release_token" \
      bash "$helper/scripts/mint-release-tag.sh"
else
  note "- immutable tag: already present at exact version commit."
fi
note "- immutable tag: \`${tag}\` at exact version commit."
emit_state tag-present

current_phase="wait-tag-release"
if ! release_seconds="$(budget_for "$release_timeout")"; then
  exit 1
fi
release_run="$(
  hard_timeout "$release_seconds" env \
    TAG="$tag" \
    VERSION_COMMIT="$version_commit" \
    RELEASE_WORKFLOW="$release_workflow" \
    KIN_ACTIONS_TOKEN="$actions_token" \
    KIN_RELEASE_RUN_TIMEOUT_SECONDS="$release_seconds" \
      bash "$helper/scripts/wait-tag-release-run.sh"
)"
release_run_id="$(jq -r .run_id <<<"$release_run")"
release_run_attempt="$(jq -r .attempt <<<"$release_run")"
note "- tag Release gate: exact ${release_workflow} run ${release_run_id} succeeded at attempt ${release_run_attempt}."

# The tag workflow, not recovery, owns public Release creation. Refresh only
# after its exact run succeeds so a queued or failed validation can never be
# converted into a finalized Release by the reconciler.
current_phase="verify-release"
release="$(
  run_bounded "$api_timeout" \
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
emit_state release-finalized

body="$(jq -r '.body // ""' <<<"$release")"
if has_marker "$downstream_marker"; then
  emit_state downstreams-dispatched
  emit_state complete
  note "- all post-publish markers already present; no recovery work needed."
  exit 0
fi
if ! has_marker "$smoke_marker"; then
  current_phase="mark-consumer-proof"
  append_marker "$smoke_marker"
  note "- fresh consumer proof: durably marked."
fi

if has_marker "$downstream_marker"; then
  note "- downstream delivery: durable marker already present."
else
  current_phase="dispatch-downstreams"
  if ! downstream_seconds="$(budget_for "$downstream_timeout")"; then
    exit 1
  fi
  hard_timeout "$downstream_seconds" env \
    PACKAGE="$package" \
    VERSION="$version" \
    GITHUB_SHA="$version_commit" \
    DOWNSTREAM_MANIFEST="$downstream_manifest" \
    KIN_DOWNSTREAM_DISPATCH_TOKEN="${KIN_DOWNSTREAM_DISPATCH_TOKEN:-}" \
    KIN_DISPATCH_MAX_RETRY_WAIT_SECONDS="$downstream_seconds" \
      bash "$helper/scripts/dispatch-downstreams.sh"
  current_phase="mark-downstreams"
  append_marker "$downstream_marker"
  note "- downstream delivery: dispatched and durably marked."
fi

emit_state downstreams-dispatched
current_phase="complete"
emit_state complete
note ""
note "COMPLETE: publication, consumer proof, exact tag, finalized Release, and downstream marker are reconciled."
