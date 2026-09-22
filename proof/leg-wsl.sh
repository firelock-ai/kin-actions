#!/usr/bin/env bash
# One WSL MCP leg, run inside WSL after install-linux.sh.
#
# Builds the fixture on the WSL ext4 home (as the docs direct, not under
# /mnt/c) and admits it with kin init, has kin setup write Claude Code's MCP
# config file, then drives the server that config names with the official MCP
# SDK client from inside WSL and writes the receipt to ~/receipts. It stops the
# daemons, keeps the captures, and exits with the client's verdict. Kin commands
# write to files, never to a pipe, for the reason leg-windows.sh gives.
#
# Environment: INSTALL_MODE, PROOF_KIN_VERSION.
set -uo pipefail
. "$HOME/proof-env.sh"
here="$(cd "$(dirname "$0")" && pwd)"
receipts="$HOME/receipts"
captures="$receipts/captures-wsl2-$INSTALL_MODE"
mkdir -p "$captures"

node "$here/make-fixture.mjs" "$HOME/kin-fixture" | tee "$HOME/fixture.json"
status=0
(cd "$HOME/kin-fixture" && kin init > "$HOME/kin-init.txt" 2>&1 < /dev/null) || status=$?
echo "$status" > "$HOME/kin-init.status"
cat "$HOME/kin-init.txt"
echo "kin init exited $status"

# Claude Code is detected from a seeded settings file; the app is not installed
# and never runs here.
mkdir -p "$HOME/.claude"
[ -f "$HOME/.claude/settings.json" ] || printf '{}\n' > "$HOME/.claude/settings.json"
status=0
(cd "$HOME/kin-fixture" && kin setup --no-interactive --intent agent --shell bash > "$HOME/kin-setup.txt" 2>&1 < /dev/null) || status=$?
echo "$status" > "$HOME/kin-setup.status"
cat "$HOME/kin-setup.txt"
echo "kin setup exited $status"
kin setup status --json > "$HOME/kin-setup-status.json" 2>&1 < /dev/null || true
cat "$HOME/.claude.json" 2>/dev/null || echo "no ~/.claude.json was written"

verdict=0
node "$here/mcp-proof.mjs" \
  --leg wsl2 --install-mode "$INSTALL_MODE" --kin-version "$PROOF_KIN_VERSION" \
  --repo "$HOME/kin-fixture" \
  --launch config --config "$HOME/.claude.json" \
  --binary "$HOME/.kin/bin/kin" \
  --install-json "$HOME/install.json" \
  --init-status "$HOME/kin-init.status" \
  --setup-status "$HOME/kin-setup.status" \
  --out "$receipts/wsl2-$INSTALL_MODE.json" || verdict=$?

kin daemon stop --all --json > "$HOME/kin-daemon-stop.json" 2>&1 < /dev/null || true
for f in install.json fixture.json kin-init.txt kin-setup.txt kin-setup-status.json kin-daemon-stop.json kin-install/npx-version.txt candidate/transfer.env .claude.json; do
  if [ -f "$HOME/$f" ]; then cp "$HOME/$f" "$captures/$(basename "$f" | sed 's/^\.//')"; fi
done
exit "$verdict"
