#!/usr/bin/env bash
set -euo pipefail

# Advance at most one exact consumer. Every repository is cloned and validated
# before the first branch, label, PR, or auto-merge write is attempted.

: "${GH_TOKEN:?GH_TOKEN is required}"
: "${TARGET_VERSION:?TARGET_VERSION is required}"
: "${KIN_ACTIONS_ROOT:?KIN_ACTIONS_ROOT is required}"
: "${PIN_MANIFEST:?PIN_MANIFEST is required}"
: "${RUNNER_TEMP:?RUNNER_TEMP is required}"

planner="${KIN_ACTIONS_ROOT}/scripts/workflow-pin-rollout.py"
protection_proof="${KIN_ACTIONS_ROOT}/scripts/workflow-pin-protection.py"
landing_proof="${KIN_ACTIONS_ROOT}/scripts/check-workflow-pin-landing.py"
main_proof="${KIN_ACTIONS_ROOT}/scripts/check-workflow-pin-main.py"
pr_proof="${KIN_ACTIONS_ROOT}/scripts/check-workflow-pin-pr.py"
reconciler="${KIN_ACTIONS_ROOT}/scripts/reconcile-workflow-pin-pr.sh"
for helper in "$planner" "$protection_proof" "$landing_proof" "$main_proof" "$pr_proof" "$reconciler"; do
  if [[ ! -f "$helper" ]]; then
    echo "workflow-pin helper is unavailable: $helper" >&2
    exit 1
  fi
done

manifest_state="$(python3 "$planner" validate --manifest "$PIN_MANIFEST")"
minimum_version="$(jq -r '.protocol.minimum_version' "$PIN_MANIFEST")"
protocol_enabled="$(python3 - "$TARGET_VERSION" "$minimum_version" <<'PY'
import re
import sys

def version(raw):
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", raw):
        raise SystemExit(f"invalid stable version: {raw}")
    return tuple(map(int, raw.split(".")))

print("true" if version(sys.argv[1]) >= version(sys.argv[2]) else "false")
PY
)"
if [[ "$protocol_enabled" != "true" ]]; then
  echo "kin-actions v${TARGET_VERSION} predates the staged rollout protocol; no-op"
  exit 0
fi

checkouts="${RUNNER_TEMP}/kin-actions-pin-preflight-${GITHUB_RUN_ID:-local}"
mkdir -p "$checkouts"
mapfile -t repositories < <(
  jq -r '.sequence[].repository' <<<"$manifest_state"
)
for repository in "${repositories[@]}"; do
  target="${checkouts}/${repository//\//__}"
  if [[ -e "$target" ]]; then
    echo "preflight checkout path already exists: $target" >&2
    exit 1
  fi
  gh repo clone "$repository" "$target" -- --filter=blob:none --depth=1
done

inventory_file="${checkouts}/inventory.json"
python3 "$planner" preflight \
  --manifest "$PIN_MANIFEST" \
  --checkouts-root "$checkouts" \
  --target-version "$TARGET_VERSION" \
  >"$inventory_file"

# Re-read every cloned default-branch head before using it as a landed-main
# proof identity. A move between clone and proof makes the run retry later.
while IFS=$'\t' read -r repository kind relation; do
  [[ "$relation" == "target" ]] || continue
  checkout="${checkouts}/${repository//\//__}"
  local_head="$(git -C "$checkout" rev-parse HEAD)"
  default_branch="$(gh api "repos/${repository}" --jq .default_branch)"
  live_head="$(gh api "repos/${repository}/commits/${default_branch}" --jq .sha)"
  if [[ "$local_head" != "$live_head" ]]; then
    echo "${repository} default branch moved during global preflight" >&2
    exit 1
  fi
done < <(
  jq -r '.repositories[] | [.repository,.kind,.relation] | @tsv' "$inventory_file"
)

proofs_file="${checkouts}/proofs.json"
printf '{}\n' >"$proofs_file"
while IFS=$'\t' read -r repository kind relation; do
  [[ "$relation" == "target" ]] || continue
  checkout="${checkouts}/${repository//\//__}"
  head_sha="$(git -C "$checkout" rev-parse HEAD)"
  default_branch="$(gh api "repos/${repository}" --jq .default_branch)"
  landing_args=(
    --repository "$repository"
    --base "$default_branch"
    --head-branch "$(jq -r '.protocol.pin_branch' "$PIN_MANIFEST")"
    --target-version "$TARGET_VERSION"
    --default-head "$head_sha"
    --checkout "$checkout"
    --kind "$kind"
    --required-app-id "$(jq -r '.protocol.required_check_app_id' "$PIN_MANIFEST")"
  )
  while IFS= read -r allowed_path; do
    landing_args+=(--allowed-path "$allowed_path")
  done < <(
    jq -r --arg repository "$repository" \
      '.repositories[$repository].workflow_paths[]' "$PIN_MANIFEST"
  )
  landing="$(
    python3 "$landing_proof" "${landing_args[@]}"
  )"
  state="$(jq -r .status <<<"$landing")"
  if [[ "$state" == "proven" && "$kind" == "cargo_release" ]]; then
    proof="$(
      python3 "$main_proof" \
        --repository "$repository" \
        --workflow "$(jq -r '.protocol.main_workflow' "$PIN_MANIFEST")" \
        --branch "$default_branch" \
        --head-sha "$(jq -r .merge_sha <<<"$landing")"
    )"
    state="$(jq -r .status <<<"$proof")"
  fi
  next="${proofs_file}.next"
  jq --arg repository "$repository" --arg state "$state" \
    '. + {($repository): $state}' "$proofs_file" >"$next"
  mv "$next" "$proofs_file"
done < <(
  jq -r '.repositories[] | [.repository,.kind,.relation] | @tsv' "$inventory_file"
)

plan="$(
  python3 "$planner" plan \
    --manifest "$PIN_MANIFEST" \
    --inventory "$inventory_file" \
    --proofs "$proofs_file"
)"
status="$(jq -r .status <<<"$plan")"
case "$status" in
  complete)
    echo "all exact kin-actions v${TARGET_VERSION} consumer pins are proven"
    exit 0
    ;;
  waiting-main-proof|blocked-main-proof|waiting-landing-proof|blocked-landing-proof)
    echo "$(jq -r .repository <<<"$plan") is ${status}; no later consumer was touched"
    exit 0
    ;;
  blocked-activation)
    echo "$(jq -r .repository <<<"$plan") is blocked before mutation: $(jq -r .blocker <<<"$plan")"
    exit 0
    ;;
  reconcile)
    ;;
  *)
    echo "unknown rollout plan status: $status" >&2
    exit 1
    ;;
esac

repository="$(jq -r .repository <<<"$plan")"
kind="$(jq -r .kind <<<"$plan")"
checkout="${checkouts}/${repository//\//__}"
# Protection is staged with the rollout. Every already-landed repository is
# re-proved by the landing helper, and the one selected next is checked here
# before its branch, label, PR, or auto-merge state can change. A deliberately
# later policy migration cannot suppress the one-repository pilot.
python3 "$protection_proof" \
  --repository "$repository" \
  --kind "$kind" \
  --required-app-id "$(jq -r '.protocol.required_check_app_id' "$PIN_MANIFEST")" \
  >/dev/null
# Inventory needs only the default-branch snapshot. The selected reconciler
# needs complete ancestry to prove its protected train branch.
if [[ "$(git -C "$checkout" rev-parse --is-shallow-repository)" == "true" ]]; then
  git -C "$checkout" fetch --unshallow --filter=blob:none origin
fi
reconcile_output="$(
  cd "$checkout"
  TARGET_REPOSITORY="$repository" \
    "$reconciler"
)"
candidate="$(tail -n 1 <<<"$reconcile_output")"
if [[ "$(jq -r .status <<<"$candidate")" != "candidate" ]]; then
  echo "${repository} produced no exact pin candidate; waiting for the next run"
  exit 0
fi

admission="$(
  python3 "$pr_proof" \
    --repository "$repository" \
    --pr "$(jq -r .pr <<<"$candidate")" \
    --base "$(jq -r .base <<<"$candidate")" \
    --base-sha "$(jq -r .base_sha <<<"$candidate")" \
    --head-branch "$(jq -r .head_branch <<<"$candidate")" \
    --head-sha "$(jq -r .head_sha <<<"$candidate")" \
    --kind "$kind" \
    --required-app-id "$(jq -r '.protocol.required_check_app_id' "$PIN_MANIFEST")"
)"
if [[ "$(jq -r .status <<<"$admission")" == "waiting" ]]; then
  echo "exact-head checks are still pending for ${repository}; auto-merge remains off"
  exit 0
fi
if [[ "$(jq -r .status <<<"$admission")" != "ready" ]]; then
  echo "unexpected exact-head admission result for ${repository}" >&2
  exit 1
fi

pr="$(jq -r .pr <<<"$candidate")"
head_sha="$(jq -r .head_sha <<<"$candidate")"
gh pr merge "$pr" --repo "$repository" \
  --auto --squash --match-head-commit "$head_sha"
echo "armed proof-complete exact-head workflow-pin PR #${pr} in ${repository}"
