#!/usr/bin/env bash
# Prepares the WSL user's side of the proof: Node inside WSL, verified against
# nodejs.org's own SHASUMS256.txt (the docs ask for Node 20 or newer inside WSL2
# for the npx path), and the proof client copied from <proof-dir> onto the WSL
# ext4 home and installed there. Writes ~/proof-env.sh for later steps.
#
# Usage: wsl-prepare.sh <proof-dir>   (a path readable from inside WSL)
# Environment: NODE_VERSION (for example v22.23.2).
set -euo pipefail
src="$1"
cd /tmp
file="node-$NODE_VERSION-linux-x64.tar.xz"
curl -fsSL --retry 3 -o "$file" "https://nodejs.org/dist/$NODE_VERSION/$file"
curl -fsSL --retry 3 -o SHASUMS256.txt "https://nodejs.org/dist/$NODE_VERSION/SHASUMS256.txt"
grep " $file\$" SHASUMS256.txt | sha256sum -c -
mkdir -p "$HOME/.local/node"
tar -xJf "$file" -C "$HOME/.local/node" --strip-components=1
printf 'export PATH="%s/.kin/bin:%s/.local/node/bin:$PATH"\n' "$HOME" "$HOME" > "$HOME/proof-env.sh"
. "$HOME/proof-env.sh"
node --version

mkdir -p "$HOME/proof" "$HOME/receipts"
cp "$src"/*.mjs "$src"/*.sh "$src/package.json" "$src/package-lock.json" "$HOME/proof/"
cd "$HOME/proof"
npm ci --no-audit --no-fund
