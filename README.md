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

- release-affecting source or dependency changes to a registry-published crate require a Cargo version change; docs, tests, comments, and CI-only changes do not;
- a `main` commit that moves the package version publishes that version to the Kin cargo registry; later non-release commits do not retag it;
- the published crate is verified from a fresh registry-only consumer;
- downstream repositories receive a `kin-registry-release` repository dispatch;
- downstream repositories open signed-off dependency bump PRs and run their smoke command.

Registry publication, release-tag minting, and downstream dispatch are separate
durable stages. A transient dispatch failure is retried with bounded backoff and
does not misreport an already verified publish or exact release tag as undone;
rerun only the failed dispatch job. Dispatch delivery is at-least-once, so
dependency waves serialize on one coalescing branch and resolve stale events to
the newest visible registry version before writing.

Each Kin repository should keep only a thin workflow wrapper and repo-local config.
Callers should pin reusable workflows to a semver tag, for example
`firelock-ai/kin-actions/.github/workflows/cargo-registry-release.yml@v0.1.22`.

## Reusable Workflows

- `.github/workflows/cargo-registry-release.yml`
  Enforces version movement, builds without local patches, publishes, verifies the exact published version, and dispatches downstreams.

- `.github/workflows/cargo-dependency-wave.yml`
  Handles `kin-registry-release` events and scheduled backstops by updating Cargo registry dependency pins and opening signed-off PRs. Server-created commits use the `github-actions[bot]` identity for truthful automation provenance.

- `.github/workflows/public-history-hygiene.yml`
  Compatibility path for the public metadata safety gate. It blocks private assistant-session references and internal tracker links before publication. The gate is validation-only: it never rewrites Git history, dates, authors, committers, or attribution, and it does not evaluate timestamps or attribution. Tool-specific attribution is optional and is not required by this action. The legacy `check-timestamps` input is accepted and ignored. Consume it from a repository PR workflow:

  ```yaml
  jobs:
    hygiene:
      uses: firelock-ai/kin-actions/.github/workflows/public-history-hygiene.yml@v0.1.22
  ```

## Required Secrets

- `KINLAB_CARGO_TOKEN`
  Required only for publish jobs.

- `KIN_CI_BOT_TOKEN`
  Preferred for downstream PR creation and repository dispatch because PRs created by the default `GITHUB_TOKEN` may not trigger all workflows.

- `KIN_RELEASE_BOT_APP_ID` and `KIN_RELEASE_BOT_PRIVATE_KEY`
  Preferred when `mint-release-tag` is enabled. The workflow mints a
  short-lived installation token instead of granting broad workflow
  permissions.

The `publish`, `mint_release_tag`, and `dispatch_downstreams` jobs all bind to
the caller's `publish-environment` (default: `registry-publish`). Put release
credentials in that main-only environment. GitHub environment secrets with the
same names override secrets mapped by the caller, while the reusable workflow
continues to accept caller-mapped secrets during migration.

Migrate without interrupting unattended releases:

1. Configure the environment's branch policy for `main` and do not add required
   reviewers to an unattended release environment.
2. Populate the environment secrets.
3. Prove a main release can publish, mint its exact tag, and dispatch.
4. Only then remove repository- or organization-scoped compatibility copies.

## License

[Apache-2.0](LICENSE).
