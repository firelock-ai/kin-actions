#!/usr/bin/env bash
set -euo pipefail

# Commit the regenerated allowlisted paths, update exactly one first-party
# release PR without force/deletion, and arm exact-head squash auto-merge.
#
# Required environment:
#   GH_TOKEN, GITHUB_REPOSITORY, MAIN_SHA, MAIN_BRANCH, TRAIN_BRANCH,
#   KIN_RELEASE_RECONCILE_SCRIPT, PR_TITLE, PR_BODY_FILE, PR_LABELS
#
# Positional arguments are the exact generated-path allowlist.

: "${GH_TOKEN:?GH_TOKEN is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${MAIN_SHA:?MAIN_SHA is required}"
: "${MAIN_BRANCH:?MAIN_BRANCH is required}"
: "${TRAIN_BRANCH:?TRAIN_BRANCH is required}"
: "${KIN_RELEASE_RECONCILE_SCRIPT:?KIN_RELEASE_RECONCILE_SCRIPT is required}"
: "${PR_TITLE:?PR_TITLE is required}"
: "${PR_BODY_FILE:?PR_BODY_FILE is required}"
: "${PR_LABELS:?PR_LABELS is required}"

if (($# == 0)); then
  echo "at least one generated path is required" >&2
  exit 1
fi
if [[ ! -f "$PR_BODY_FILE" ]]; then
  echo "PR body file is missing" >&2
  exit 1
fi

generated_args=()
for path in "$@"; do
  generated_args+=(--generated-path "$path")
done

git add -- "$@"
if ! git diff-files --quiet --ignore-submodules=none; then
  echo "release generator left unstaged tracked changes" >&2
  exit 1
fi
if [[ -n "$(git ls-files --others --exclude-standard)" ]]; then
  echo "release generator left untracked files" >&2
  exit 1
fi

while IFS= read -r -d '' changed; do
  allowed=false
  for generated in "$@"; do
    if [[ "$changed" == "$generated" ]]; then
      allowed=true
      break
    fi
  done
  if [[ "$allowed" != "true" ]]; then
    echo "release generator staged non-allowlisted path: $changed" >&2
    exit 1
  fi
done < <(git diff --cached --name-only -z)

git config user.name "kin-release[bot]"
git config user.email "kin-release[bot]@users.noreply.github.com"
if ! git diff --cached --quiet; then
  git commit -s -m "$PR_TITLE"
fi

head="$(git rev-parse HEAD)"
python3 "$KIN_RELEASE_RECONCILE_SCRIPT" validate-train \
  --trusted-main "$MAIN_SHA" \
  --train-head "$head" \
  "${generated_args[@]}" \
  >/dev/null

plain_remote="https://github.com/${GITHUB_REPOSITORY}.git"
token_remote="https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
git remote set-url origin "$token_remote"
restore_remote() {
  git remote set-url origin "$plain_remote" 2>/dev/null || true
}
trap restore_remote EXIT
git push origin "HEAD:refs/heads/${TRAIN_BRANCH}"
restore_remote
trap - EXIT

IFS=',' read -ra labels <<<"$PR_LABELS"
for raw in "${labels[@]}"; do
  label="$(xargs <<<"$raw")"
  [[ -n "$label" ]] || continue
  gh label create "$label" --color "5319e7" \
    --description "Kin automatic release authority" \
    --repo "$GITHUB_REPOSITORY" >/dev/null 2>&1 || true
done

prs="$(
  gh pr list --repo "$GITHUB_REPOSITORY" --state open \
    --base "$MAIN_BRANCH" --head "$TRAIN_BRANCH" \
    --json number,headRefOid,headRepositoryOwner
)"
count="$(jq 'length' <<<"$prs")"
if ((count > 1)); then
  echo "multiple open PRs claim the one release train branch" >&2
  exit 1
fi
if ((count == 0)); then
  create_args=()
  for raw in "${labels[@]}"; do
    label="$(xargs <<<"$raw")"
    [[ -n "$label" ]] && create_args+=(--label "$label")
  done
  gh pr create --repo "$GITHUB_REPOSITORY" \
    --base "$MAIN_BRANCH" --head "$TRAIN_BRANCH" \
    --title "$PR_TITLE" --body-file "$PR_BODY_FILE" \
    "${create_args[@]}" >/dev/null
  prs="$(
    gh pr list --repo "$GITHUB_REPOSITORY" --state open \
      --base "$MAIN_BRANCH" --head "$TRAIN_BRANCH" \
      --json number,headRefOid,headRepositoryOwner
  )"
fi

pr="$(jq -r '.[0].number' <<<"$prs")"
remote_head="$(jq -r '.[0].headRefOid' <<<"$prs")"
owner="$(jq -r '.[0].headRepositoryOwner.login // ""' <<<"$prs")"
if [[ "$remote_head" != "$head" ]]; then
  echo "PR #$pr head is $remote_head, expected generated head $head" >&2
  exit 1
fi
if [[ "$owner" != "${GITHUB_REPOSITORY%%/*}" ]]; then
  echo "PR #$pr is not first-party: owner is $owner" >&2
  exit 1
fi

edit_args=()
for raw in "${labels[@]}"; do
  label="$(xargs <<<"$raw")"
  [[ -n "$label" ]] && edit_args+=(--add-label "$label")
done
gh pr edit "$pr" --repo "$GITHUB_REPOSITORY" \
  --title "$PR_TITLE" --body-file "$PR_BODY_FILE" \
  "${edit_args[@]}" >/dev/null
gh pr merge "$pr" --repo "$GITHUB_REPOSITORY" \
  --auto --squash --match-head-commit "$head"

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  {
    echo "pull_request=$pr"
    echo "train_head=$head"
  } >> "$GITHUB_OUTPUT"
fi
echo "armed exact-head auto-merge for release PR #$pr at $head"
