#!/usr/bin/env bash
# Installs Kin inside WSL for one leg.
#
# The reference archive is either a local file already delivered into WSL
# (REF_FILE) or a URL this script downloads (REF_URL). Either way its sha256
# must equal REF_SHA256 before it is extracted or installed.
#
# archive: installs the reference archive the way its INSTALL.md says, placing
#          kin and kin-daemon in ~/.kin/bin.
# npx:     runs npx -y @kinlab/kin@PROOF_KIN_VERSION, which provisions the managed
#          binary, and compares it against the reference archive when there is one.
#
# Environment: INSTALL_MODE, SOURCE_KIND, REF_FILE or REF_URL, REF_SHA256,
# PROOF_KIN_VERSION, and for a delivered file TRANSFER_KIND, TRANSFER_REF,
# TRANSFER_NOTE. Writes ~/install.json.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
work="$HOME/kin-install"
mkdir -p "$work/archive"
bin_dir="$HOME/.kin/bin"

file=""
if [ -n "${REF_FILE:-}" ]; then
  file="$REF_FILE"
  [ -f "$file" ] || { echo "::error::the delivered archive $REF_FILE does not exist" >&2; exit 1; }
elif [ -n "${REF_URL:-}" ]; then
  file="$work/$(basename "$REF_URL")"
  curl -fsSL --retry 3 -o "$file" "$REF_URL"
fi

actual="" archive_kin="" kin_in_archive="" refused=false
if [ -n "$file" ]; then
  actual="$(sha256sum "$file" | awk '{print $1}')"
  if [ "$actual" = "${REF_SHA256:-}" ]; then
    tar -xzf "$file" -C "$work/archive"
    kin_in_archive="$(find "$work/archive" -type f -name kin | head -n 1)"
    [ -n "$kin_in_archive" ] || { echo "::error::no kin binary inside the reference archive" >&2; exit 1; }
    archive_kin="$(sha256sum "$kin_in_archive" | awk '{print $1}')"
  else
    # Refused before anything is extracted, let alone installed.
    refused=true
  fi
fi

case "$INSTALL_MODE" in
  archive)
    source_kind="$SOURCE_KIND"
    if [ -z "$file" ]; then
      echo "::error::the archive leg has no reference archive" >&2
      exit 1
    elif [ "$refused" = false ]; then
      mkdir -p "$bin_dir"
      cp "$kin_in_archive" "$(dirname "$kin_in_archive")/kin-daemon" "$bin_dir/"
      how="verified the reference archive's sha256 inside WSL, then copied kin and kin-daemon into ~/.kin/bin as the archive INSTALL.md says"
    else
      how="refused the reference archive before extracting it: its sha256 is $actual, expected ${REF_SHA256:-none}"
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
HOW="$how" KIND="$source_kind" DOWNLOADED="$actual" REFUSED="$refused" ARCHIVE_KIN="$archive_kin" \
INSTALLED="$installed" INSTALLED_SHA="$installed_sha" BINARY_NAME=kin \
REF_FILE="${REF_FILE:-}" REF_URL="${REF_URL:-}" \
OUT="$HOME/install.json" node "$here/write-install.mjs"
if [ "$INSTALL_MODE" = archive ] && [ "$refused" = true ]; then
  echo "::error::$how" >&2
  exit 1
fi
