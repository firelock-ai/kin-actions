#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Firelock, LLC

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
check="${root}/scripts/check-actions-cache-policy.sh"
fixtures="$(mktemp -d)"
trap 'rm -rf "${fixtures}"' EXIT

make_case() {
  local name="$1"
  local case_root="${fixtures}/${name}"
  mkdir -p "${case_root}/.github/workflows" "${case_root}/.github/actions"
  cp "${root}"/.github/workflows/*.yml "${case_root}/.github/workflows/"
  if [[ -d "${root}/.github/actions" ]]; then
    cp -R "${root}/.github/actions/." "${case_root}/.github/actions/"
  fi
  printf '%s\n' "${case_root}"
}

expect_rejection() {
  local name="$1"
  local case_root="$2"
  local expected="$3"
  local output
  if output="$("${check}" "${case_root}/.github/workflows" "${case_root}/.github/actions" 2>&1)"; then
    echo "FAIL: ${name} falsifier was accepted" >&2
    exit 1
  fi
  if ! grep -Fq "${expected}" <<<"${output}"; then
    echo "FAIL: ${name} failed for the wrong reason" >&2
    printf '%s\n' "${output}" >&2
    exit 1
  fi
  echo "OK: ${name} rejected"
}

"${check}" "${root}/.github/workflows" "${root}/.github/actions"

case_root="$(make_case target-output)"
perl -0pi -e 's#(~/.cargo/git\n)#$1            target\n#' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection target-output "${case_root}" "target output is forbidden"

case_root="$(make_case dynamic-lock-key)"
perl -0pi -e "s/cargo-source-v1-\\\$\\{\\{ runner.os \\}\\}-\\\$\\{\\{ runner.arch \\}\\}/cargo-source-\\\$\\{\\{ hashFiles('**\\/Cargo.lock') \\}\\}/" "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection dynamic-lock-key "${case_root}" "cache keys must not expand"

case_root="$(make_case dynamic-ref-key)"
perl -0pi -e 's/cargo-source-v1-\$\{\{ runner\.os \}\}-\$\{\{ runner\.arch \}\}/cargo-source-\$\{\{ github.ref \}\}/' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection dynamic-ref-key "${case_root}" "cache keys must not expand"

case_root="$(make_case missing-arch-epoch)"
perl -0pi -e 's/cargo-source-v1-\$\{\{ runner\.os \}\}-\$\{\{ runner\.arch \}\}/cargo-source-v1-\$\{\{ runner.os \}\}/' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection missing-arch-epoch "${case_root}" "restore key must be the bounded OS and architecture epoch"

case_root="$(make_case restore-prefix)"
perl -0pi -e 's/(          key: cargo-source-v1-\$\{\{ runner\.os \}\}-\$\{\{ runner\.arch \}\}\n)/$1          restore-keys: cargo-source-v1-\n/' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection restore-prefix "${case_root}" "restore-keys are forbidden"

case_root="$(make_case lookup-only)"
perl -0pi -e 's/(          key: cargo-source-v1-\$\{\{ runner\.os \}\}-\$\{\{ runner\.arch \}\}\n)/$1          lookup-only: true\n/' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection lookup-only "${case_root}" "lookup-only, fail-on-cache-miss"

case_root="$(make_case fail-on-cache-miss)"
perl -0pi -e 's/(          key: cargo-source-v1-\$\{\{ runner\.os \}\}-\$\{\{ runner\.arch \}\}\n)/$1          fail-on-cache-miss: true\n/' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection fail-on-cache-miss "${case_root}" "lookup-only, fail-on-cache-miss"

case_root="$(make_case non-main-save)"
perl -0pi -e "s/if: success\\(\\) && github.event_name == 'push' && github.ref == 'refs\\/heads\\/main' && steps.cargo_source_cache.outputs.cache-hit != 'true'/if: always()/" "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection non-main-save "${case_root}" "cache save must be restricted to a main push cache miss"

case_root="$(make_case continue-on-error-build)"
perl -0pi -e 's/(      - name: Build without local patches\n)/$1        continue-on-error: true\n/' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection continue-on-error-build "${case_root}" "cache save must not follow a continue-on-error step"

case_root="$(make_case partial-step-after-build)"
perl -0pi -e 's/(      - name: Save bounded cargo source cache from main\n)/      - name: Partial fetch after build\n        run: cargo fetch || true\n$1/' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection partial-step-after-build "${case_root}" "cache save must immediately follow the authoritative registry build"

case_root="$(make_case masked-build-failure)"
perl -0pi -e 's/(      - name: Build without local patches\n        env:\n          REGISTRY_BUILD_COMMAND: .*?\n        run: \|\n)          set -euo pipefail\n/$1          set +e\n/s' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection masked-build-failure "${case_root}" "authoritative registry build must fail closed with set -euo pipefail"

case_root="$(make_case monolithic-action)"
perl -0pi -e 's#actions/cache/restore\@55cc8345863c7cc4c66a329aec7e433d2d1c52a9#actions/cache\@55cc8345863c7cc4c66a329aec7e433d2d1c52a9#' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection monolithic-action "${case_root}" "use exact pinned"

case_root="$(make_case mutable-cache-action)"
perl -0pi -e 's#actions/cache/restore\@55cc8345863c7cc4c66a329aec7e433d2d1c52a9#actions/cache/restore\@v6#' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection mutable-cache-action "${case_root}" "use exact pinned"

case_root="$(make_case uppercase-cache-action)"
perl -0pi -e 's#actions/cache/restore\@55cc8345863c7cc4c66a329aec7e433d2d1c52a9#Actions/cache/restore\@55cc8345863c7cc4c66a329aec7e433d2d1c52a9#' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection uppercase-cache-action "${case_root}" "use exact pinned"

case_root="$(make_case escaped-cache-action)"
perl -0pi -e 's#uses: actions/cache/restore\@55cc8345863c7cc4c66a329aec7e433d2d1c52a9#uses: "actions/\\u0063ache/restore@55cc8345863c7cc4c66a329aec7e433d2d1c52a9"#' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection escaped-cache-action "${case_root}" "use exact pinned"

case_root="$(make_case conditional-restore)"
perl -0pi -e 's/(      - name: Restore bounded cargo source cache\n)/$1        "if" : github.event_name == '\''pull_request'\''\n/' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection conditional-restore "${case_root}" "cache restore must run on every workflow ref"

case_root="$(make_case early-save)"
perl -0pi -e '
  if (s#\n(      - name: Save bounded cargo source cache from main\n.*?          key: \$\{\{ steps\.cargo_source_cache\.outputs\.cache-primary-key \}\}\n)#$save = $1; "\n"#se) {
    s#(      - name: Force registry-only Kin config\n)#$save$1#;
  }
' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection early-save "${case_root}" "cache save must be the last declared job step"

case_root="$(make_case duplicate-condition-key)"
perl -0pi -e 's/(      - name: Restore bounded cargo source cache\n)/$1        if: true\n        if: false\n/' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection duplicate-condition-key "${case_root}" "duplicate YAML mapping key"

case_root="$(make_case remote-composite)"
perl -0pi -e 's#actions/cache/restore\@55cc8345863c7cc4c66a329aec7e433d2d1c52a9#vendor/cache-action\@0123456789012345678901234567890123456789#' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection remote-composite "${case_root}" "remote or repo-local composite actions are forbidden"

case_root="$(make_case local-composite-cache)"
mkdir -p "${case_root}/.github/actions/hidden-cache"
printf '%s\n' \
  'name: Hidden cache' \
  'runs:' \
  '  using: composite' \
  '  steps:' \
  '    - uses: actions/cache@v6' \
  '      with:' \
  '        path: target' \
  '        key: hidden' \
  >"${case_root}/.github/actions/hidden-cache/action.yml"
expect_rejection local-composite-cache "${case_root}" "repo-local composite actions must not invoke actions/cache"

case_root="$(make_case setup-cache-input)"
perl -0pi -e 's/(      - uses: actions\/setup-python\@a26af69be951a213d495a4c3e4e4022e16d87065 # v5\.6\.0\n        with:\n          python-version: "3\.11"\n)/$1          cache: pip\n/' "${case_root}/.github/workflows/self-test.yml"
expect_rejection setup-cache-input "${case_root}" "setup-action cache inputs"

case_root="$(make_case missing-policy-enforcement)"
perl -0pi -e 's#\n      - name: Enforce bounded Actions cache authority\n        id: actions_cache_policy\n        run: \|\n          \./scripts/check-actions-cache-policy\.sh\n          \./scripts/test-actions-cache-policy\.sh\n##' "${case_root}/.github/workflows/self-test.yml"
expect_rejection missing-policy-enforcement "${case_root}" "must contain exactly one actions_cache_policy enforcement step"

case_root="$(make_case softened-policy-enforcement)"
perl -0pi -e 's/(        id: actions_cache_policy\n)/$1        continue-on-error: true\n/' "${case_root}/.github/workflows/self-test.yml"
expect_rejection softened-policy-enforcement "${case_root}" "must be unconditional and failure-authoritative"

case_root="$(make_case reusable-job-replacement)"
perl -0pi -e 's/(  test:\n.*?    runs-on: ubuntu-latest\n)    steps:/$1    uses: vendor\/workflow\/.github\/workflows\/test.yml\@0123456789012345678901234567890123456789\n    steps:/s' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection reusable-job-replacement "${case_root}" "must not delegate to a reusable workflow"

echo "OK: all Actions cache policy falsifiers were rejected."
