#!/usr/bin/env bash
# One native Windows MCP leg, run after install-windows.sh (Git Bash).
#
# Builds the fixture and admits it with kin init, has kin setup write Claude
# Code's MCP config file, then drives the server that config names with the
# official MCP SDK client and writes the receipt. It always stops the daemons
# and keeps the captures, and exits with the client's verdict.
#
# Kin commands run with their output redirected to files, never piped: a daemon
# started by a command inherits its handles, and a pipe it holds would keep the
# caller waiting until the daemon idles out. Their exit statuses go to files the
# client grades, so a failure here still yields a receipt.
#
# Environment: INSTALL_MODE, PROOF_KIN_VERSION, RUNNER_TEMP; RECEIPTS_DIR
# (default ./receipts); GITHUB_STEP_SUMMARY when present.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
T="$(cygpath -u "$RUNNER_TEMP")"
receipts="${RECEIPTS_DIR:-receipts}"
captures="$receipts/captures-windows-native-$INSTALL_MODE"
mkdir -p "$captures"
export PATH="$HOME/.kin/bin:$PATH"

# The documented native Windows step: Kin admits only a worktree whose bytes
# match the committed tree.
git config --global core.autocrlf false
node "$here/make-fixture.mjs" "$(cygpath -w "$T/kin-fixture")" | tee "$T/fixture.json"
status=0
(cd "$T/kin-fixture" && kin init > "$T/kin-init.txt" 2>&1 < /dev/null) || status=$?
echo "$status" > "$T/kin-init.status"
cat "$T/kin-init.txt"
echo "kin init exited $status"

# Claude Code keeps ~/.claude/settings.json, and kin setup detects Claude Code
# from it. The Claude Code app is not installed and never runs here; only the
# config file kin setup writes is under test.
mkdir -p "$HOME/.claude"
[ -f "$HOME/.claude/settings.json" ] || printf '{}\n' > "$HOME/.claude/settings.json"
status=0
(cd "$T/kin-fixture" && kin setup --no-interactive --intent agent --shell powershell > "$T/kin-setup.txt" 2>&1 < /dev/null) || status=$?
echo "$status" > "$T/kin-setup.status"
cat "$T/kin-setup.txt"
echo "kin setup exited $status"
kin setup status --json > "$T/kin-setup-status.json" 2>&1 < /dev/null || true
cat "$HOME/.claude.json" 2>/dev/null || echo "no ~/.claude.json was written"

verdict=0
node "$here/mcp-proof.mjs" \
  --leg windows-native --install-mode "$INSTALL_MODE" --kin-version "$PROOF_KIN_VERSION" \
  --repo "$(cygpath -w "$T/kin-fixture")" \
  --launch config --config "$(cygpath -w "$HOME/.claude.json")" \
  --binary "$(cygpath -w "$HOME/.kin/bin/kin.exe")" \
  --install-json "$(cygpath -w "$T/install.json")" \
  --init-status "$(cygpath -w "$T/kin-init.status")" \
  --setup-status "$(cygpath -w "$T/kin-setup.status")" \
  --out "$receipts/windows-native-$INSTALL_MODE.json" || verdict=$?

kin daemon stop --all --json > "$T/kin-daemon-stop.json" 2>&1 < /dev/null || true
for f in runner.json install.json fixture.json kin-init.txt kin-setup.txt kin-setup-status.json kin-daemon-stop.json kin-install/npx-version.txt; do
  if [ -f "$T/$f" ]; then cp "$T/$f" "$captures/"; fi
done
if [ -f "$HOME/.claude.json" ]; then cp "$HOME/.claude.json" "$captures/claude.json"; fi
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then node "$here/summarize.mjs" "$receipts"/*.json >> "$GITHUB_STEP_SUMMARY" || true; fi
exit "$verdict"
