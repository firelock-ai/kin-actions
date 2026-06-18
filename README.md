# kin-actions

Shared GitHub Actions workflows for Kin registry releases and dependency waves.

Current release: `v0.1.0`.

The release contract is:

- source changes to a registry-published crate require a Cargo version change;
- pushes to `main` publish that version to the Kin cargo registry;
- the published crate is verified from a fresh registry-only consumer;
- downstream repositories receive a `kin-registry-release` repository dispatch;
- downstream repositories open signed-off dependency bump PRs and run their smoke command.

Each Kin repository should keep only a thin workflow wrapper and repo-local config.
Callers should pin reusable workflows to a semver tag, for example
`firelock-ai/kin-actions/.github/workflows/cargo-registry-release.yml@v0.1.1`.

## Reusable Workflows

- `.github/workflows/cargo-registry-release.yml`
  Enforces version movement, builds without local patches, publishes, verifies the exact published version, and dispatches downstreams.

- `.github/workflows/cargo-dependency-wave.yml`
  Handles `kin-registry-release` events and scheduled backstops by updating Cargo registry dependency pins and opening signed-off PRs.

## Required Secrets

- `KINLAB_CARGO_TOKEN`
  Required only for publish jobs.

- `KIN_CI_BOT_TOKEN`
  Preferred for downstream PR creation and repository dispatch because PRs created by the default `GITHUB_TOKEN` may not trigger all workflows.
