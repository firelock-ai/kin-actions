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
- pushes to `main` publish that version to the Kin cargo registry;
- the published crate is verified from a fresh registry-only consumer;
- downstream repositories receive a `kin-registry-release` repository dispatch;
- downstream repositories open signed-off dependency bump PRs and run their smoke command.

Each Kin repository should keep only a thin workflow wrapper and repo-local config.
Callers should pin reusable workflows to a semver tag, for example
`firelock-ai/kin-actions/.github/workflows/cargo-registry-release.yml@v0.1.20`.

## Reusable Workflows

- `.github/workflows/cargo-registry-release.yml`
  Enforces version movement, builds without local patches, publishes, verifies the exact published version, and dispatches downstreams.

- `.github/workflows/cargo-dependency-wave.yml`
  Handles `kin-registry-release` events and scheduled backstops by updating Cargo registry dependency pins and opening signed-off PRs. Server-created commits use the `github-actions[bot]` identity for truthful automation provenance.

- `.github/workflows/public-history-hygiene.yml`
  Compatibility path for the public metadata safety gate. It blocks private assistant-session references and internal tracker links before publication. The gate is validation-only: it never rewrites Git history, dates, authors, committers, or attribution, and it does not evaluate timestamps or attribution. The legacy `check-timestamps` input is accepted and ignored. Consume it from a repository PR workflow:

  ```yaml
  jobs:
    hygiene:
      uses: firelock-ai/kin-actions/.github/workflows/public-history-hygiene.yml@v0.1.20
  ```

## Required Secrets

- `KINLAB_CARGO_TOKEN`
  Required only for publish jobs.

- `KIN_CI_BOT_TOKEN`
  Preferred for downstream PR creation and repository dispatch because PRs created by the default `GITHUB_TOKEN` may not trigger all workflows.

## License

[Apache-2.0](LICENSE).
