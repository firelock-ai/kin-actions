#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Firelock, LLC

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
workflow_root="${1:-${root}/.github/workflows}"
action_root="${2:-$(dirname "${workflow_root}")/actions}"

exec ruby "${root}/scripts/actions-cache-policy.rb" "${workflow_root}" "${action_root}"
