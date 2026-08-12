#!/usr/bin/env bash
set -euo pipefail

# Reconcile one manifest-allowlisted consumer pin PR with the dedicated App.
# Unlike the general release App, this App is installed only on the consumer
# manifest repositories and carries Workflows write because the generated
# bytes are exact `uses: ...@vX.Y.Z` lines under .github/workflows.

: "${GH_TOKEN:?GH_TOKEN is required}"
: "${TARGET_REPOSITORY:?TARGET_REPOSITORY is required}"
: "${TARGET_VERSION:?TARGET_VERSION is required}"
: "${PIN_MANIFEST:?PIN_MANIFEST is required}"
: "${KIN_ACTIONS_ROOT:?KIN_ACTIONS_ROOT is required}"

# This default is coupled to `--pin-branch` in scripts/check-version-bump.py,
# which exempts this branch's chore PRs from release-label bump forcing. The
# gate runs inside the consumer repo and cannot observe an override here, so
# overriding PIN_BRANCH without moving that default puts every pin PR back into
# the unmergeable state this pairing exists to prevent.
PIN_BRANCH="${PIN_BRANCH:-automation/kin-actions-pin-next}"
# One identity for the local commits and for the sign-off trailer carried by the
# server-authored merge, so a future rename cannot desync them.
PIN_BOT_NAME="kin-workflow-pin[bot]"
PIN_BOT_EMAIL="kin-workflow-pin[bot]@users.noreply.github.com"
if [[ ! "$TARGET_REPOSITORY" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  echo "unsafe target repository: $TARGET_REPOSITORY" >&2
  exit 1
fi
if [[ ! "$TARGET_VERSION" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
  echo "target version must be stable numeric SemVer" >&2
  exit 1
fi
if [[ ! "$PIN_BRANCH" =~ ^automation/[A-Za-z0-9._/-]+$ ]]; then
  echo "unsafe pin branch: $PIN_BRANCH" >&2
  exit 1
fi

wrapper="${KIN_ACTIONS_ROOT}/scripts/reconcile-workflow-pin-branch.py"
updater="${KIN_ACTIONS_ROOT}/scripts/update-kin-actions-pins.py"
resolver="${KIN_ACTIONS_ROOT}/scripts/resolve-workflow-pin-pr.py"
if [[ ! -f "$wrapper" || ! -f "$updater" || ! -f "$resolver" ||
      ! -f "$PIN_MANIFEST" ]]; then
  echo "workflow-pin helper or manifest is unavailable" >&2
  exit 1
fi
if [[ -n "$(git status --porcelain=v1 --untracked-files=all)" ]]; then
  echo "consumer checkout must be clean" >&2
  exit 1
fi

main_branch="$(
  gh api "repos/${TARGET_REPOSITORY}" --jq .default_branch
)"
plain_remote="https://github.com/${TARGET_REPOSITORY}.git"
token_remote="https://x-access-token:${GH_TOKEN}@github.com/${TARGET_REPOSITORY}.git"
git remote set-url origin "$token_remote"
restore_remote() {
  git remote set-url origin "$plain_remote" 2>/dev/null || true
}
trap restore_remote EXIT

git fetch --no-tags origin \
  "refs/heads/${main_branch}:refs/remotes/kin-pin/trusted-main"
main_sha="$(git rev-parse refs/remotes/kin-pin/trusted-main)"
live_main="$(gh api "repos/${TARGET_REPOSITORY}/commits/${main_branch}" --jq .sha)"
if [[ "$main_sha" != "$live_main" ]]; then
  echo "consumer default branch moved during pin admission" >&2
  exit 1
fi

pin_ref="refs/heads/${PIN_BRANCH}"
matching="$(
  gh api "repos/${TARGET_REPOSITORY}/git/matching-refs/heads/${PIN_BRANCH}"
)"
pin_sha="$(
  jq -r --arg ref "$pin_ref" \
    '[.[] | select(.ref == $ref)][0].object.sha // ""' <<<"$matching"
)"
if [[ -z "$pin_sha" ]]; then
  jq -n --arg ref "$pin_ref" --arg sha "$main_sha" \
    '{ref:$ref,sha:$sha}' \
    | gh api --method POST "repos/${TARGET_REPOSITORY}/git/refs" --input - \
      >/dev/null
  pin_sha="$main_sha"
fi

git fetch --no-tags origin \
  "refs/heads/${PIN_BRANCH}:refs/remotes/kin-pin/train"
if [[ "$(git rev-parse refs/remotes/kin-pin/train)" != "$pin_sha" ]]; then
  echo "pin branch moved during admission" >&2
  exit 1
fi
git switch --detach "$pin_sha"
git switch -c kin-workflow-pin-local

validation="$(
  python3 "$wrapper" validate-train \
    --manifest "$PIN_MANIFEST" \
    --repository "$TARGET_REPOSITORY" \
    --trusted-main "$main_sha" \
    --train-head "$pin_sha"
)"
if ! git merge-base --is-ancestor "$main_sha" "$pin_sha"; then
  git config user.name "$PIN_BOT_NAME"
  git config user.email "$PIN_BOT_EMAIL"
  if (($(jq '.changed_paths | length' <<<"$validation") > 0)); then
    neutralization="$(
      python3 "$wrapper" neutralize \
        --manifest "$PIN_MANIFEST" \
        --repository "$TARGET_REPOSITORY" \
        --trusted-main "$main_sha" \
        --old-train-head "$pin_sha"
    )"
    if [[ "$(jq -r .changed <<<"$neutralization")" == "true" ]]; then
      git commit -s -m "chore(ci): neutralize generated workflow pins"
    else
      git commit --allow-empty -s -m "chore(ci): checkpoint workflow pin train"
    fi
  else
    git commit --allow-empty -s -m "chore(ci): checkpoint workflow pin train"
  fi
  neutralized="$(git rev-parse HEAD)"
  git push origin "HEAD:refs/heads/${PIN_BRANCH}"
  # GitHub authors this merge server-side, so `git commit -s` cannot reach it
  # and consumers enforcing DCO reject the whole PR over a commit no local
  # hook ever saw. The supplied commit message is the only writable surface,
  # so carry the trailer in it, on its own final line as DCO matchers anchor.
  merge_message="$(
    printf 'chore(ci): merge trusted %s\n\nSigned-off-by: %s <%s>\n' \
      "$main_branch" "$PIN_BOT_NAME" "$PIN_BOT_EMAIL"
  )"
  merge="$(
    jq -n --arg base "$PIN_BRANCH" --arg head "$main_sha" \
      --arg message "$merge_message" \
      '{base:$base,head:$head,commit_message:$message}' \
      | gh api --method POST "repos/${TARGET_REPOSITORY}/merges" --input -
  )"
  merge_sha="$(jq -r '.sha // ""' <<<"$merge")"
  if [[ ! "$merge_sha" =~ ^[0-9a-f]{40}$ && ! "$merge_sha" =~ ^[0-9a-f]{64}$ ]]; then
    echo "GitHub did not return the exact workflow-pin merge commit" >&2
    exit 1
  fi
  # Read the trailer back off the commit GitHub actually wrote. Requesting it is
  # not the same as getting it, and every consumer enforcing DCO rejects the
  # whole PR if this silently stops working, with nothing pointing back here.
  if ! jq -r '.commit.message // ""' <<<"$merge" \
    | grep -Eq '^Signed-off-by: .+ <[^>]+>$'; then
    echo "workflow-pin merge commit ${merge_sha} carries no sign-off trailer" >&2
    exit 1
  fi
  git fetch --no-tags origin \
    "refs/heads/${PIN_BRANCH}:refs/remotes/kin-pin/server-merge"
  if [[ "$(git rev-parse refs/remotes/kin-pin/server-merge)" != "$merge_sha" ]]; then
    echo "workflow-pin branch does not match server merge response" >&2
    exit 1
  fi
  git switch --detach "$merge_sha"
  git branch -f kin-workflow-pin-local "$merge_sha"
  git switch kin-workflow-pin-local
  python3 "$wrapper" validate-merge \
    --manifest "$PIN_MANIFEST" \
    --repository "$TARGET_REPOSITORY" \
    --trusted-main "$main_sha" \
    --old-train-head "$neutralized" \
    --merge-commit "$merge_sha" \
    >/dev/null
fi

result="$(
  python3 "$updater" \
    --manifest "$PIN_MANIFEST" \
    --repository "$TARGET_REPOSITORY" \
    --version "$TARGET_VERSION"
)"
mapfile -t allowed < <(jq -r '.allowed_paths[]' <<<"$result")
git add -- "${allowed[@]}"
if ! git diff-files --quiet --ignore-submodules=none; then
  echo "workflow pin updater left unstaged tracked changes" >&2
  exit 1
fi
if [[ -n "$(git ls-files --others --exclude-standard)" ]]; then
  echo "workflow pin updater left untracked files" >&2
  exit 1
fi
while IFS= read -r -d '' changed; do
  permitted=false
  for path in "${allowed[@]}"; do
    [[ "$changed" == "$path" ]] && permitted=true
  done
  if [[ "$permitted" != "true" ]]; then
    echo "workflow pin updater staged non-manifest path: $changed" >&2
    exit 1
  fi
done < <(git diff --cached --name-only -z)

git config user.name "$PIN_BOT_NAME"
git config user.email "$PIN_BOT_EMAIL"
if ! git diff --cached --quiet; then
  git commit -s -m "chore(ci): pin kin-actions v${TARGET_VERSION}"
fi
head="$(git rev-parse HEAD)"
python3 "$wrapper" validate-train \
  --manifest "$PIN_MANIFEST" \
  --repository "$TARGET_REPOSITORY" \
  --trusted-main "$main_sha" \
  --train-head "$head" \
  >/dev/null
if [[ "$(git rev-parse "${head}^{tree}")" == "$(git rev-parse "${main_sha}^{tree}")" ]]; then
  echo "${TARGET_REPOSITORY} already pins kin-actions v${TARGET_VERSION} on trusted main"
  restore_remote
  trap - EXIT
  exit 0
fi
git push origin "HEAD:refs/heads/${PIN_BRANCH}"
restore_remote
trap - EXIT

for label in "release:automated" "release:consumer-bump"; do
  gh label create "$label" --repo "$TARGET_REPOSITORY" \
    --color "5319e7" --description "Kin automatic release authority" \
    >/dev/null 2>&1 || true
done
prs="$(
  gh pr list --repo "$TARGET_REPOSITORY" --state open \
    --base "$main_branch" --head "$PIN_BRANCH" --json number
)"
if (($(jq length <<<"$prs") > 1)); then
  echo "multiple workflow-pin PRs claim ${PIN_BRANCH}" >&2
  exit 1
fi
body_file="${RUNNER_TEMP:-/tmp}/kin-actions-pin-${TARGET_REPOSITORY##*/}.md"
{
  echo "Automatic immutable kin-actions workflow pin reconciliation."
  echo
  echo "- release: \`v${TARGET_VERSION}\`"
  echo "- trusted ${main_branch}: \`${main_sha}\`"
  echo "- changed paths: exact \`.kin-release/consumers.json\` allowlist only"
  echo
  echo "The dedicated workflow-pin App has no main or tag bypass. Ordinary required checks decide admission."
} > "$body_file"
if (($(jq length <<<"$prs") == 0)); then
  gh pr create --repo "$TARGET_REPOSITORY" \
    --base "$main_branch" --head "$PIN_BRANCH" \
    --title "chore(ci): pin kin-actions v${TARGET_VERSION}" \
    --body-file "$body_file" \
    --label "release:automated" --label "release:consumer-bump" \
    >/dev/null
fi
# The listing above decides whether a PR has to be opened, and a pull request
# object trails the ref it tracks, so its head oid is routinely the pre-push one
# a second after the push. Asserting on that single read is a race: refusals
# clustered about a second after the push, while the pushes that survived were
# read three to nine seconds later. The resolver re-reads until GitHub reports
# the commit this run pushed, and still refuses any head that is not the exact
# first-party generated one.
pr="$(
  python3 "$resolver" \
    --repository "$TARGET_REPOSITORY" \
    --base "$main_branch" \
    --head-branch "$PIN_BRANCH" \
    --expect-head "$head"
)"
gh pr edit "$pr" --repo "$TARGET_REPOSITORY" \
  --title "chore(ci): pin kin-actions v${TARGET_VERSION}" \
  --body-file "$body_file" \
  --add-label "release:automated" \
  --add-label "release:consumer-bump" \
  >/dev/null
gh pr merge "$pr" --repo "$TARGET_REPOSITORY" \
  --auto --squash --match-head-commit "$head"
echo "armed exact-head workflow-pin PR #$pr in ${TARGET_REPOSITORY}"
