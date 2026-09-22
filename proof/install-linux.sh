#!/usr/bin/env bash
# Installs Kin inside WSL for one leg.
#
# archive: downloads REF_URL, refuses it unless its sha256 is REF_SHA256, then
#          places kin and kin-daemon in ~/.kin/bin as the archive's INSTALL.md says.
# npx:     runs npx -y @kinlab/kin@PROOF_KIN_VERSION, which provisions the managed
#          binary, and compares it against REF_URL's kin when there is one.
#
# Environment: INSTALL_MODE, SOURCE_KIND, REF_URL, REF_SHA256, PROOF_KIN_VERSION.
# Writes ~/install.json.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
work="$HOME/kin-install"
mkdir -p "$work/archive"
bin_dir="$HOME/.kin/bin"

downloaded="" archive_kin="" kin_in_archive=""
if [ -n "${REF_URL:-}" ]; then
  file="$work/$(basename "$REF_URL")"
  curl -fsSL --retry 3 -o "$file" "$REF_URL"
  downloaded="$(sha256sum "$file" | awk '{print $1}')"
  tar -xzf "$file" -C "$work/archive"
  kin_in_archive="$(find "$work/archive" -type f -name kin | head -n 1)"
  [ -n "$kin_in_archive" ] || { echo "::error::no kin binary inside $REF_URL" >&2; exit 1; }
  archive_kin="$(sha256sum "$kin_in_archive" | awk '{print $1}')"
fi
matched=false
if [ -n "${REF_URL:-}" ] && [ "$downloaded" = "$REF_SHA256" ]; then matched=true; fi

case "$INSTALL_MODE" in
  archive)
    source_kind="$SOURCE_KIND"
    if [ "$matched" = true ]; then
      mkdir -p "$bin_dir"
      cp "$kin_in_archive" "$(dirname "$kin_in_archive")/kin-daemon" "$bin_dir/"
      how="downloaded $REF_URL inside WSL, verified its sha256, copied kin and kin-daemon into ~/.kin/bin as the archive INSTALL.md says"
    else
      how="refused to install $REF_URL: its sha256 is $downloaded, expected $REF_SHA256"
    fi
    ;;
  npx)
    source_kind=npm
    status=0
    npx -y "@kinlab/kin@$PROOF_KIN_VERSION" --version > "$work/npx-version.txt" 2>&1 < /dev/null || status=$?
    cat "$work/npx-version.txt"
    [ "$status" -eq 0 ]
    how="ran npx -y @kinlab/kin@$PROOF_KIN_VERSION --version inside WSL, which provisions the managed binary into ~/.kin/bin"
    ;;
  *)
    echo "::error::unknown install mode $INSTALL_MODE" >&2
    exit 1
    ;;
esac

installed=""
installed_sha=""
if [ -f "$bin_dir/kin" ]; then
  installed="$bin_dir/kin"
  installed_sha="$(sha256sum "$bin_dir/kin" | awk '{print $1}')"
fi
HOW="$how" KIND="$source_kind" DOWNLOADED="$downloaded" ARCHIVE_KIN="$archive_kin" \
INSTALLED="$installed" INSTALLED_SHA="$installed_sha" BINARY_NAME=kin \
OUT="$HOME/install.json" node "$here/write-install.mjs"
if [ "$INSTALL_MODE" = archive ] && [ "$matched" != true ]; then
  echo "::error::$how" >&2
  exit 1
fi
