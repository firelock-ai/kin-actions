# kin-actions

> Shared GitHub Actions workflows for Kin registry releases and dependency waves.

Reusable workflows for enforcing version movement, publishing to the Kin cargo
registry, verifying published crates, and keeping downstream dependency pins in
sync. Each Kin repository keeps a thin workflow wrapper and pins a semver tag of
`kin-actions`, so this repo is the central release-enforcement substrate.

This is shared CI and release infrastructure for the Kin ecosystem, not a product
surface. In the ecosystem manifest (`kin/docs/ecosystem-manifest.json`) it is
`layer: infrastructure`, `role: shared-ci-and-release-workflows`.

Published versions are listed on the
[GitHub tags page](https://github.com/firelock-ai/kin-actions/tags).

[![Part of Kin](https://img.shields.io/badge/part%20of-Kin-6E56CF.svg)](https://github.com/firelock-ai/kin)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

## What is Kin?

Kin is the semantic repo and collaboration substrate: the system of record for
AI-written software. Your code lives as a graph of entities, relations, and intents,
not a pile of files and diffs. AI agents and humans navigate it semantically, with
provenance, review, and governance built in. It coexists with Git and projects graph
truth back to a normal filesystem, so any tool works unchanged.

Start at **[firelock-ai/kin](https://github.com/firelock-ai/kin)** · **[kinlab.ai](https://kinlab.ai)**

## Release contract

- Ordinary source PRs do not hand-edit package versions in train mode. Trusted
  `main` drift is coalesced into one protected `automation/release-next` PR.
- Source drift defaults to a patch. A `Kin-Release-Intent: minor` or
  `Kin-Release-Intent: major` trailer on a landed first-parent commit escalates
  intent; the highest immutable trailer since the last release wins. Mutable
  PR labels have zero release-intent authority.
- The train changes only the exact package manifest and tracked root lockfile
  discovered from Cargo metadata. Manual version edits and extra generated-PR
  paths fail closed.
- Only the generated version-moving `main` commit can publish. A tag delivery
  never re-enters Cargo publication.
- Publication is followed by a fresh registry-only consumer build before the
  immutable tag and downstream notification are admitted.
- Dependency and workflow-pin waves update every inventoried live consumer by
  protected, signed automation PR; they try the full inventory before reporting
  a partial failure.

Registry publication, release-tag minting, GitHub Release creation, and
downstream dispatch are separate durable stages. The automatic recovery
controller resumes only missing post-publish stages, never republishes or moves
a tag, and maintains one terminal failure issue per package. A transient
dispatch failure is retried with bounded backoff and does not misreport an
already verified publish or exact release tag as undone. Dispatch delivery is
at-least-once, so
dependency waves serialize on one coalescing branch and resolve stale events to
the newest visible registry version before writing. One retry-wait deadline
covers the complete downstream manifest, and a non-empty manifest fails closed
when no dispatch credential is configured.

Each Kin repository should keep only a thin workflow wrapper and repo-local config.
Callers should pin reusable workflows to a semver tag, for example
`firelock-ai/kin-actions/.github/workflows/cargo-registry-release.yml@v0.1.22`.

## Full-auto Cargo caller

The release train is notification-independent: every surviving run re-reads the
last immutable tag and all first-parent `main` commits since it. A replaced
pending event therefore cannot lose release intent. Install the combined
template at `.kin-release/release-train-caller.yml` as
`.github/workflows/release-train.yml`; it triggers train work after CI and
recovery after the registry workflow, with scheduled and typed-dispatch
backstops. No human dispatch is required:

```yaml
name: Cargo release train

on:
  workflow_run:
    workflows: [CI, Registry Publish]
    types: [completed]
  repository_dispatch:
    types: [kin-cargo-release-reconcile]
  schedule:
    - cron: "17,47 * * * *"

jobs:
  train:
    if: >-
      github.event_name != 'workflow_run' ||
      (github.event.workflow_run.name == 'CI' &&
       github.event.workflow_run.conclusion == 'success')
    uses: firelock-ai/kin-actions/.github/workflows/cargo-release-train.yml@vX.Y.Z
    with:
      package: kin-example
      required-workflow: CI
    secrets: inherit

  recovery:
    if: >-
      github.event_name != 'workflow_run' ||
      github.event.workflow_run.name == 'Registry Publish'
    uses: firelock-ai/kin-actions/.github/workflows/cargo-release-recovery.yml@vX.Y.Z
    with:
      package: kin-example
      required-workflow: CI
    secrets: inherit
```

The existing registry wrapper then opts into train-owned version authority and
automatic tag minting:

```yaml
jobs:
  release:
    uses: firelock-ai/kin-actions/.github/workflows/cargo-registry-release.yml@vX.Y.Z
    with:
      package: kin-example
      version-mode: train
      mint-release-tag: true
    secrets: inherit
```

`vX.Y.Z` means the released version containing these workflows; callers must
replace it with an immutable numeric tag. Until the App credentials, protected
environments, auto-merge, and required checks below are configured, the
controllers stop safely instead of weakening admission.

If the caller also consumes Kin crates while its own version is train-owned,
its dependency-wave wrapper must pass `version-mode: train`,
`bump-own-version: false`, and `secrets: inherit`. The general release App then
authors the dependency branch and PR. Train mode rejects the default Actions
token and rejects any attempt to bump the caller's own version.

### Two-release activation

Activation is deliberately A → callers → inventory → B:

1. Release Kin Actions A with these controllers while
   `.kin-release/consumers.json` still lists only already-live workflow pins.
2. In all eight Cargo repositories listed in
   `.kin-release/cargo-train-bootstrap.json`, pin the registry wrapper and new
   `.github/workflows/release-train.yml` to A, pass `secrets: inherit`, enable
   train mode/tag minting, and apply the four recorded
   `bump-own-version: false` dependency-wave changes.
3. Land and verify all eight caller PRs. Do not expand the consumer inventory
   before those exact paths exist.
4. Add the eight now-live release-train paths to `consumers.json`, then release
   Kin Actions B.
5. B's pin wave updates every old and new live pin to B. Missing or premature
   inventory entries fail closed without partial writes.

## Reusable Workflows

- `.github/workflows/cargo-release-train.yml`
  Reconstructs cumulative trusted drift, prepares the exact Cargo version and
  lockfile bytes, and enables squash auto-merge on the exact protected head.

- `.github/workflows/cargo-registry-release.yml`
  Enforces manual or train-owned version authority, builds without local
  patches, publishes, verifies the exact published version, mints its tag, and
  dispatches downstreams.

- `.github/workflows/cargo-release-recovery.yml`
  Automatically resumes consumer proof, exact tag, GitHub Release, and durable
  downstream delivery after a Cargo version is already present in the registry.

- `.github/workflows/cargo-dependency-wave.yml`
  Handles `kin-registry-release` events and scheduled backstops by updating Cargo registry dependency pins and opening signed-off PRs. Server-created commits use the `github-actions[bot]` identity for truthful automation provenance.

- `.github/workflows/self-release-train.yml`
  Coalesces changes in this repository into an exact `VERSION`, `README.md`, and
  `CONTRIBUTING.md` release PR.

- `.github/workflows/release.yml`
  Converts the exact tested `VERSION` on `main` into one immutable tag and
  finalized GitHub Release, then asks the pin controller to reconcile consumers.

- `.github/workflows/pin-wave.yml`
  Compares the latest finalized `kin-actions` release with every live pin in
  `.kin-release/consumers.json` and opens or updates exact pin PRs.

- `.github/workflows/public-history-hygiene.yml`
  Compatibility path for the public metadata safety gate. It blocks private assistant-session references and internal tracker links before publication. The gate is validation-only: it never rewrites Git history, dates, authors, committers, or attribution, and it does not evaluate timestamps or attribution. Tool-specific attribution is optional and is not required by this action. The legacy `check-timestamps` input is accepted and ignored. Consume it from a repository PR workflow:

  ```yaml
  jobs:
    hygiene:
      uses: firelock-ai/kin-actions/.github/workflows/public-history-hygiene.yml@v0.1.22
  ```

## Activation requirements

- `KINLAB_CARGO_TOKEN`
  Required in each Cargo caller's `registry-publish` environment.

- `KIN_CI_BOT_TOKEN`
  Compatibility credential for downstream PR creation and repository dispatch.

- `KIN_RELEASE_BOT_APP_ID` and `KIN_RELEASE_BOT_PRIVATE_KEY`
  Put these in Cargo callers' `registry-publish` environments and this
  repository's `release-tag` environment. Install the App on the current
  repository with Contents, Pull requests, and Issues read/write. The general
  release App intentionally has no Workflows permission or `main` bypass. It
  needs narrowly scoped bypasses for its exact `automation/release-next` branch
  and for tag creation, but not for the overlapping tag-freeze rules described
  below.

- `KIN_WORKFLOW_PIN_APP_ID` and `KIN_WORKFLOW_PIN_APP_PRIVATE_KEY`
  Put these only in this repository's `release-followups` environment. This is
  a separate App installed on exactly the repositories in
  `.kin-release/consumers.json`, with Contents, Pull requests, Issues, and
  Workflows read/write. The controller verifies that installation inventory
  before it writes.

For unattended operation:

1. Enable repository auto-merge and retain required checks and branch
   protections on `main`; neither App receives a `main` bypass. Give the
   general App the exact `automation/release-next` branch bypass and an exact
   `automation/kin-registry-dependency-wave` branch bypass. If a consumer protects
   automation branches, give the pin App only the exact
   `automation/kin-actions-pin-next` branch bypass there.
2. Split version-tag control into overlapping rulesets: the release App may
   bypass the creation ruleset so it can mint a new tag, while a second freeze
   ruleset blocks tag update, deletion, and non-fast-forward without any
   release-App bypass. Keep only founder break-glass on the freeze ruleset.
3. Restrict `registry-publish`, `release-tag`, and `release-followups` to the
   default branch. Do not put required reviewers on an unattended release
   environment.
4. Populate the environment secrets and install each App with only the
   permissions above.
5. Configure squash merges to retain the PR title and body. Put
   `Kin-Release-Intent: minor` or `Kin-Release-Intent: major` at the end of the
   PR body when escalation is required, and verify the landed first-parent
   commit preserved it.
6. Add the immediate and scheduled caller wrappers, then verify one generated
   PR traverses checks, publish, fresh-consumer proof, tag, GitHub Release, and
   downstream reconciliation.
7. Remove compatibility PATs only after that end-to-end proof.

## Recovery and rollback

The controllers reconcile durable Git, registry, tag, Release, and live-pin
state, so rerunning a failed stage is safe. If a release is bad, publish a fixed
forward version; for Cargo, yank the bad version when appropriate. Never move a
tag, reuse an immutable registry version, force-rewrite a train branch, or edit
consumer history. Pin the fixed version through the same protected wave.

## License

[Apache-2.0](LICENSE).
