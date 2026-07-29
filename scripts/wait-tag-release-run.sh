#!/usr/bin/env bash
set -euo pipefail

# Wait for (and, within a strict attempt bound, rerun) the caller's exact
# tag-triggered Release workflow. This preserves the caller's validation gate;
# it never creates a GitHub Release itself.

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${TAG:?TAG is required}"
: "${VERSION_COMMIT:?VERSION_COMMIT is required}"
: "${RELEASE_WORKFLOW:?RELEASE_WORKFLOW is required}"
: "${KIN_ACTIONS_TOKEN:?KIN_ACTIONS_TOKEN is required}"

timeout_seconds="${KIN_RELEASE_RUN_TIMEOUT_SECONDS:-900}"
poll_seconds="${KIN_RELEASE_RUN_POLL_SECONDS:-10}"
max_attempts="${KIN_RELEASE_RUN_MAX_ATTEMPTS:-3}"
for pair in \
  "timeout:$timeout_seconds" \
  "poll:$poll_seconds" \
  "attempts:$max_attempts"; do
  name="${pair%%:*}"
  value="${pair#*:}"
  if [[ ! "$value" =~ ^[0-9]+$ ]]; then
    echo "${name} bound must be a non-negative integer, got ${value}" >&2
    exit 1
  fi
done
if ((max_attempts < 1)); then
  echo "attempt bound must be at least one" >&2
  exit 1
fi

started="$SECONDS"
rerun_attempt=0
poll_bound_seconds="$poll_seconds"
if ((poll_bound_seconds < 1)); then poll_bound_seconds=1; fi
max_polls=$((timeout_seconds / poll_bound_seconds + 2))
poll_count=0
while ((SECONDS - started <= timeout_seconds && poll_count < max_polls)); do
  ((poll_count += 1))
  runs="$(
    GH_TOKEN="$KIN_ACTIONS_TOKEN" gh run list \
      --repo "$GITHUB_REPOSITORY" \
      --workflow "$RELEASE_WORKFLOW" \
      --commit "$VERSION_COMMIT" \
      --event push \
      --limit 100 \
      --json attempt,conclusion,createdAt,databaseId,event,headBranch,headSha,status,url,workflowName
  )"
  run="$(
    jq -c \
      --arg tag "$TAG" \
      --arg commit "$VERSION_COMMIT" \
      --arg workflow "$RELEASE_WORKFLOW" \
      '
        [
          .[]
          | select(
              .event == "push"
              and .headBranch == $tag
              and .headSha == $commit
              and .workflowName == $workflow
            )
        ]
        | sort_by(.createdAt, .databaseId)
        | last // {}
      ' <<<"$runs"
  )"
  run_id="$(jq -r '.databaseId // ""' <<<"$run")"
  if [[ -z "$run_id" ]]; then
    sleep "$poll_seconds"
    continue
  fi

  status="$(jq -r '.status // ""' <<<"$run")"
  conclusion="$(jq -r '.conclusion // ""' <<<"$run")"
  attempt="$(jq -r '.attempt // 0' <<<"$run")"
  url="$(jq -r '.url // ""' <<<"$run")"
  if [[ ! "$attempt" =~ ^[0-9]+$ ]] || ((attempt < 1)); then
    echo "Release run ${run_id} returned invalid attempt ${attempt}" >&2
    exit 1
  fi

  if [[ "$status" != "completed" ]]; then
    sleep "$poll_seconds"
    continue
  fi
  if [[ "$conclusion" == "success" ]]; then
    jq -cn \
      --argjson run_id "$run_id" \
      --argjson attempt "$attempt" \
      --arg url "$url" \
      '{run_id:$run_id,attempt:$attempt,url:$url}'
    exit 0
  fi
  if ((attempt >= max_attempts)); then
    echo "Release run ${run_id} ended ${conclusion} at bounded attempt ${attempt}: ${url}" >&2
    exit 1
  fi
  if ((rerun_attempt == attempt)); then
    sleep "$poll_seconds"
    continue
  fi

  echo "Release run ${run_id} ended ${conclusion}; requesting bounded full rerun $((attempt + 1))/${max_attempts}" >&2
  GH_TOKEN="$KIN_ACTIONS_TOKEN" gh run rerun "$run_id" \
    --repo "$GITHUB_REPOSITORY"
  rerun_attempt="$attempt"
  sleep "$poll_seconds"
done

echo "exact ${TAG} ${RELEASE_WORKFLOW} run did not succeed within ${timeout_seconds}s" >&2
exit 1
