#!/usr/bin/env bash
# Installs Kin on native Windows (Git Bash) for one leg.
#
# archive: downloads REF_URL, refuses it unless its sha256 is REF_SHA256, then
#          places kin.exe and kin-daemon.exe where the archive's INSTALL.md says.
# npx:     runs npx -y @kinlab/kin@PROOF_KIN_VERSION, which provisions the managed
#          binary, and compares it against REF_URL's kin.exe when there is one.
#
# Environment: INSTALL_MODE, SOURCE_KIND, REF_URL, REF_SHA256, PROOF_KIN_VERSION,
# RUNNER_TEMP, GITHUB_PATH. Writes $RUNNER_TEMP/install.json.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
T="$(cygpath -u "$RUNNER_TEMP")"
work="$T/kin-install"
mkdir -p "$work/archive"
bin_dir="$HOME/.kin/bin"

downloaded="" archive_kin="" kin_in_archive=""
if [ -n "${REF_URL:-}" ]; then
  file="$work/$(basename "$REF_URL")"
  curl -fsSL --retry 3 -o "$file" "$REF_URL"
  downloaded="$(sha256sum "$file" | awk '{print $1}')"
  # Windows' own tar reads both zip and tar.gz.
  "$(cygpath -u "$SYSTEMROOT")/System32/tar.exe" -xf "$(cygpath -w "$file")" -C "$(cygpath -w "$work/archive")"
  kin_in_archive="$(find "$work/archive" -type f -name kin.exe | head -n 1)"
  [ -n "$kin_in_archive" ] || { echo "::error::no kin.exe inside $REF_URL" >&2; exit 1; }
  archive_kin="$(sha256sum "$kin_in_archive" | awk '{print $1}')"
fi
matched=false
if [ -n "${REF_URL:-}" ] && [ "$downloaded" = "$REF_SHA256" ]; then matched=true; fi

case "$INSTALL_MODE" in
  archive)
    source_kind="$SOURCE_KIND"
    if [ "$matched" = true ]; then
      mkdir -p "$bin_dir"
      cp "$kin_in_archive" "$(dirname "$kin_in_archive")/kin-daemon.exe" "$bin_dir/"
      how="downloaded $REF_URL, verified its sha256, copied kin.exe and kin-daemon.exe into %USERPROFILE%\\.kin\\bin as the archive INSTALL.md says"
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
    how="ran npx -y @kinlab/kin@$PROOF_KIN_VERSION --version, which provisions the managed binary into %USERPROFILE%\\.kin\\bin"
    ;;
  *)
    echo "::error::unknown install mode $INSTALL_MODE" >&2
    exit 1
    ;;
esac

installed=""
installed_sha=""
if [ -f "$bin_dir/kin.exe" ]; then
  installed="$(cygpath -w "$bin_dir/kin.exe")"
  installed_sha="$(sha256sum "$bin_dir/kin.exe" | awk '{print $1}')"
  cygpath -w "$bin_dir" >> "$GITHUB_PATH"
fi
HOW="$how" KIND="$source_kind" DOWNLOADED="$downloaded" ARCHIVE_KIN="$archive_kin" \
INSTALLED="$installed" INSTALLED_SHA="$installed_sha" BINARY_NAME=kin.exe \
OUT="$(cygpath -w "$T/install.json")" node "$here/write-install.mjs"
if [ "$INSTALL_MODE" = archive ] && [ "$matched" != true ]; then
  echo "::error::$how" >&2
  exit 1
fi
