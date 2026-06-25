> **Umbrella guidance:** the workspace-root `AGENTS.md` is the source of truth for cross-repo thesis, boundaries, and rules. This file is the repo-specific authority for `kin-actions`.

# kin-actions

Shared GitHub Actions reusable workflows for the Kin registry release pipeline.

## Contents

- `cargo-registry-release.yml` — reusable release workflow; called by `registry-publish.yml` in each crate repo.
- `cargo-dependency-wave.yml` — reusable dependency-wave workflow; called by `kin-dependency-wave.yml` in consumer repos.

## Boundary rule

Put work here when it changes shared CI behavior across repos. Version strictly:
any breaking change to the workflow interface requires a semver bump and a
consumer pin-wave across all repos that reference a `@vX.Y.Z` tag.

## Version cadence

- Consumers pin to `@vX.Y.Z` refs.
- Bump `VERSION` and add a tag when cutting a release.
- After each new version, the captain runs a pin-wave to update all consumer refs.
