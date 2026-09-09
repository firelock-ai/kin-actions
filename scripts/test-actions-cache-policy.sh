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

expect_acceptance() {
  local name="$1"
  local case_root="$2"
  if ! "${check}" "${case_root}/.github/workflows" "${case_root}/.github/actions" >/dev/null; then
    echo "FAIL: ${name} valid fixture was rejected" >&2
    exit 1
  fi
  echo "OK: ${name} accepted"
}

"${check}" "${root}/.github/workflows" "${root}/.github/actions"

case_root="$(make_case target-output)"
perl -0pi -e 's#(~/.cargo/git\n)#$1            target\n#' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection target-output "${case_root}" "target output is forbidden"

case_root="$(make_case dynamic-lock-key)"
perl -0pi -e "s/cargo-source-v2-\\\$\\{\\{ runner.os \\}\\}-\\\$\\{\\{ runner.arch \\}\\}/cargo-source-\\\$\\{\\{ hashFiles('**\\/Cargo.lock') \\}\\}/" "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection dynamic-lock-key "${case_root}" "cache keys must not expand"

case_root="$(make_case dynamic-ref-key)"
perl -0pi -e 's/cargo-source-v2-\$\{\{ runner\.os \}\}-\$\{\{ runner\.arch \}\}/cargo-source-\$\{\{ github.ref \}\}/' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection dynamic-ref-key "${case_root}" "cache keys must not expand"

case_root="$(make_case missing-arch-epoch)"
perl -0pi -e 's/cargo-source-v2-\$\{\{ runner\.os \}\}-\$\{\{ runner\.arch \}\}/cargo-source-v2-\$\{\{ runner.os \}\}/' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection missing-arch-epoch "${case_root}" "restore key must be the bounded OS and architecture epoch"

case_root="$(make_case restore-prefix)"
perl -0pi -e 's/(          key: cargo-source-v2-\$\{\{ runner\.os \}\}-\$\{\{ runner\.arch \}\}\n)/$1          restore-keys: cargo-source-v2-\n/' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection restore-prefix "${case_root}" "lookup-only, fail-on-cache-miss"

case_root="$(make_case lookup-only)"
perl -0pi -e 's/(          key: cargo-source-v2-\$\{\{ runner\.os \}\}-\$\{\{ runner\.arch \}\}\n)/$1          lookup-only: true\n/' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection lookup-only "${case_root}" "lookup-only, fail-on-cache-miss"

case_root="$(make_case fail-on-cache-miss)"
perl -0pi -e 's/(          key: cargo-source-v2-\$\{\{ runner\.os \}\}-\$\{\{ runner\.arch \}\}\n)/$1          fail-on-cache-miss: true\n/' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection fail-on-cache-miss "${case_root}" "lookup-only, fail-on-cache-miss"

case_root="$(make_case non-main-save)"
perl -0pi -e "s/if: success\\(\\) && steps.cargo_source_fetch.outcome == 'success' && github.event_name == 'push' && github.ref == 'refs\\/heads\\/main' && steps.cargo_source_cache.outputs.cache-hit != 'true'/if: always()/" "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection non-main-save "${case_root}" "cache save must be restricted to a successful main-push fetch"

case_root="$(make_case restore-continue-on-error)"
perl -0pi -e 's/(          key: cargo-source-v2-\$\{\{ runner\.os \}\}-\$\{\{ runner\.arch \}\}\n)/$1        continue-on-error: true\n/' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection restore-continue-on-error "${case_root}" "keys must be exactly"

case_root="$(make_case save-continue-on-error-after-with)"
perl -0pi -e 's/(          key: \$\{\{ steps\.cargo_source_cache\.outputs\.cache-primary-key \}\}\n)/$1        continue-on-error: true\n/' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection save-continue-on-error-after-with "${case_root}" "keys must be exactly"

case_root="$(make_case masked-fetch-failure)"
perl -0pi -e 's/cargo "\$\{fetch_args\[@\]\}"\n/cargo "\$\{fetch_args\[@\]\}" || true\n/' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection masked-fetch-failure "${case_root}" "cache save authority must be the exact fail-closed Cargo fetch"

case_root="$(make_case deleted-manifest-containment)"
perl -0pi -e 's/          if \[\[ ! -f "\$MANIFEST" \|\| -L "\$MANIFEST" \]\]; then\n            echo "Cargo manifest must be a regular non-symlink file" >&2\n            exit 1\n          fi\n//' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection deleted-manifest-containment "${case_root}" "cache save authority must be the exact fail-closed Cargo fetch"

case_root="$(make_case softened-fetch-shell)"
perl -0pi -e 's/(      - name: Fetch complete cargo source graph\n.*?        run: \|\n)          set -euo pipefail\n/$1          set +e\n/s' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection softened-fetch-shell "${case_root}" "cache save authority must be the exact fail-closed Cargo fetch"

case_root="$(make_case cached-root-target-dir)"
perl -0pi -e 's#CARGO_TARGET_DIR: \$\{\{ runner.temp \}\}/kin-actions-target#CARGO_TARGET_DIR: /home/runner/.cargo/git/target#' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection cached-root-target-dir "${case_root}" "cache save authority must be the exact fail-closed Cargo fetch"

case_root="$(make_case deleted-config-symlink-guard)"
perl -0pi -e 's/          if \[\[ -L \.cargo \|\| -L \.cargo\/config\.toml \]\]; then\n            echo "Cargo config path must not be a symlink" >&2\n            exit 1\n          fi\n//' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection deleted-config-symlink-guard "${case_root}" "registry config before cache save must remain exact"

case_root="$(make_case workflow-target-dir)"
perl -0pi -e 's/(env:\n  CARGO_TERM_COLOR: always\n)/$1  CARGO_TARGET_DIR: ~\/\.cargo\/git\/target\n/' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection workflow-target-dir "${case_root}" "workflow env must remain exactly"

case_root="$(make_case cargo-bash-env)"
perl -0pi -e 's/(env:\n  CARGO_TERM_COLOR: always\n)/$1  BASH_ENV: .\/scripts\/mask-failures.sh\n/' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection cargo-bash-env "${case_root}" "BASH_ENV, PATH, RUBYOPT"

case_root="$(make_case self-test-rubyopt)"
perl -0pi -e 's/(permissions:\n)/env:\n  RUBYOPT: -r.\/scripts\/mask-policy.rb\n\n$1/' "${case_root}/.github/workflows/self-test.yml"
expect_rejection self-test-rubyopt "${case_root}" "workflow env is forbidden before policy enforcement"

case_root="$(make_case partial-command-before-save)"
perl -0pi -e 's/(      - name: Save bounded cargo source cache from main\n)/      - name: Partial fetch after authority\n        run: cargo fetch || true\n$1/' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection partial-command-before-save "${case_root}" "must preserve every release proof step in exact order"

case_root="$(make_case deleted-registry-build)"
perl -0pi -e 's#\n      - name: Build without local patches\n        env:\n          PACKAGE: .*?\n          REGISTRY_BUILD_COMMAND: .*?\n          CARGO_TARGET_DIR: .*?\n        run: python3 \.kin-actions/scripts/run-registry-build\.py\n##s' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection deleted-registry-build "${case_root}" "must preserve every release proof step in exact order"

case_root="$(make_case masked-registry-build-helper)"
perl -0pi -e 's#run: python3 \.kin-actions/scripts/run-registry-build\.py#run: python3 .kin-actions/scripts/run-registry-build.py || true#' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection masked-registry-build-helper "${case_root}" "registry build must use the fail-closed typed helper"

case_root="$(make_case registry-job-condition)"
perl -0pi -e 's/(  registry_smoke:\n    name: Registry-only build\n)/$1    if: github.actor == '\''nobody'\''\n/' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection registry-job-condition "${case_root}" "job \"registry_smoke\": keys must be exactly"

case_root="$(make_case test-job-continue-on-error)"
perl -0pi -e 's/(  test:\n    name: Repo verification\n)/$1    continue-on-error: true\n/' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection test-job-continue-on-error "${case_root}" "job \"test\": keys must be exactly"

case_root="$(make_case changed-publish-condition)"
perl -0pi -e "s/needs.version_gate.outputs.release_candidate == 'true'/needs.version_gate.outputs.release_candidate != 'true'/" "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection changed-publish-condition "${case_root}" "publish condition must preserve exact release admission"

case_root="$(make_case redirected-protected-checkout)"
perl -0pi -e 's/(  registry_smoke:\n.*?      - uses: actions\/checkout\@d23441a48e516b6c34aea4fa41551a30e30af803 # v6\.1\.0\n)/$1        with:\n          ref: main\n/s' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection redirected-protected-checkout "${case_root}" "registry_smoke candidate checkout must use the exact workflow commit"

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

case_root="$(make_case duplicate-condition-key)"
perl -0pi -e 's/(      - name: Restore bounded cargo source cache\n)/$1        if: true\n        if: false\n/' "${case_root}/.github/workflows/cargo-registry-release.yml"
expect_rejection duplicate-condition-key "${case_root}" "duplicate YAML mapping key"

case_root="$(make_case opaque-remote-action)"
perl -0pi -e 's/(      - uses: actions\/setup-python\@a26af69be951a213d495a4c3e4e4022e16d87065)/      - uses: vendor\/opaque-action\@0123456789012345678901234567890123456789\n$1/' "${case_root}/.github/workflows/hygiene.yml"
expect_rejection opaque-remote-action "${case_root}" "unaudited remote action"

case_root="$(make_case remote-reusable-workflow)"
perl -0pi -e 's#uses: \./\.github/workflows/merge-queue-ejection-notice\.yml#uses: vendor/opaque/.github/workflows/reuse.yml\@0123456789012345678901234567890123456789#' "${case_root}/.github/workflows/ejection-notice.yml"
expect_rejection remote-reusable-workflow "${case_root}" "delegates to an unaudited remote reusable workflow"

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

setup_node_sha="249970729cb0ef3589644e2896645e5dc5ba9c38"
case_root="$(make_case setup-node-implicit-cache)"
perl -0pi -e "s#(      - uses: actions/setup-python)#      - uses: actions/setup-node\\@${setup_node_sha}\n        with:\n          node-version: '24'\n\$1#" "${case_root}/.github/workflows/hygiene.yml"
expect_rejection setup-node-implicit-cache "${case_root}" "setup-node must set package-manager-cache to boolean false"

case_root="$(make_case setup-node-explicit-cache)"
perl -0pi -e "s#(      - uses: actions/setup-python)#      - uses: actions/setup-node\\@${setup_node_sha}\n        with:\n          node-version: '24'\n          package-manager-cache: true\n\$1#" "${case_root}/.github/workflows/hygiene.yml"
expect_rejection setup-node-explicit-cache "${case_root}" "setup-node must set package-manager-cache to boolean false"

case_root="$(make_case setup-node-cache-disabled)"
perl -0pi -e "s#(      - uses: actions/setup-python)#      - uses: actions/setup-node\\@${setup_node_sha}\n        with:\n          node-version: '24'\n          package-manager-cache: false\n\$1#" "${case_root}/.github/workflows/hygiene.yml"
expect_acceptance setup-node-cache-disabled "${case_root}"

case_root="$(make_case composite-implicit-setup-node)"
mkdir -p "${case_root}/.github/actions/hidden-node"
printf '%s\n' \
  'name: Hidden node cache' \
  'runs:' \
  '  using: composite' \
  '  steps:' \
  "    - uses: actions/setup-node@${setup_node_sha}" \
  '      with:' \
  "        node-version: '24'" \
  >"${case_root}/.github/actions/hidden-node/action.yml"
perl -0pi -e 's/(      - uses: actions\/setup-python)/      - uses: .\/\.github\/actions\/hidden-node\n$1/' "${case_root}/.github/workflows/hygiene.yml"
expect_rejection composite-implicit-setup-node "${case_root}" "setup-node must set package-manager-cache to boolean false"

case_root="$(make_case nested-outside-composite)"
mkdir -p "${case_root}/tools/outer-action" "${case_root}/tools/inner-action"
printf '%s\n' \
  'name: Outer action' \
  'runs:' \
  '  using: composite' \
  '  steps:' \
  '    - uses: ./tools/inner-action' \
  >"${case_root}/tools/outer-action/action.yml"
printf '%s\n' \
  'name: Inner action' \
  'runs:' \
  '  using: composite' \
  '  steps:' \
  "    - uses: actions/setup-node@${setup_node_sha}" \
  >"${case_root}/tools/inner-action/action.yml"
perl -0pi -e 's/(      - uses: actions\/setup-python)/      - uses: .\/tools\/outer-action\n$1/' "${case_root}/.github/workflows/hygiene.yml"
expect_rejection nested-outside-composite "${case_root}" "setup-node must set package-manager-cache to boolean false"

case_root="$(make_case setup-cache-input)"
perl -0pi -e 's/(      - uses: actions\/setup-python\@a26af69be951a213d495a4c3e4e4022e16d87065 # v5\.6\.0\n        with:\n          python-version: "3\.11"\n)/$1          cache: pip\n/' "${case_root}/.github/workflows/self-test.yml"
expect_rejection setup-cache-input "${case_root}" "setup-action cache inputs"

case_root="$(make_case missing-policy-enforcement)"
perl -0pi -e 's#      - name: Enforce bounded Actions cache authority\n        id: actions_cache_policy\n        run: \|\n          \./scripts/check-actions-cache-policy\.sh\n          \./scripts/test-actions-cache-policy\.sh\n##' "${case_root}/.github/workflows/self-test.yml"
expect_rejection missing-policy-enforcement "${case_root}" "must run exactly one actions_cache_policy step immediately after checkout"

case_root="$(make_case softened-policy-after-run)"
perl -0pi -e 's/(          \.\/scripts\/test-actions-cache-policy\.sh\n)/$1        continue-on-error: true\n/' "${case_root}/.github/workflows/self-test.yml"
expect_rejection softened-policy-after-run "${case_root}" "actions_cache_policy: keys must be exactly"

case_root="$(make_case redirected-self-test-checkout)"
perl -0pi -e 's/(          fetch-depth: 0\n)/$1          ref: main\n/' "${case_root}/.github/workflows/self-test.yml"
expect_rejection redirected-self-test-checkout "${case_root}" "must begin with the exact candidate checkout and no ref redirection"

case_root="$(make_case conditional-self-test-job)"
perl -0pi -e 's/(  python-unit:\n    name: Script unit tests\n)/$1    if: github.actor == '\''nobody'\''\n/' "${case_root}/.github/workflows/self-test.yml"
expect_rejection conditional-self-test-job "${case_root}" "python-unit: keys must be exactly"

case_root="$(make_case softened-self-test-defaults)"
perl -0pi -e 's/(permissions:\n)/defaults:\n  run:\n    shell: bash {0} || true\n\n$1/' "${case_root}/.github/workflows/self-test.yml"
expect_rejection softened-self-test-defaults "${case_root}" "workflow defaults are forbidden"

case_root="$(make_case missing-self-test-trigger)"
perl -0pi -e 's/  merge_group:\n//' "${case_root}/.github/workflows/self-test.yml"
expect_rejection missing-self-test-trigger "${case_root}" "pull_request, push main, and merge_group triggers must remain exact"

echo "OK: all Actions cache policy falsifiers were rejected."
